"""Tests for the second-pass LLM self-review.

Self-review is now an *additional* LLM generation. The agent reads the
freshly emitted document plus its upstream context and decides
whether the document needs repair; if yes, it returns the rewritten
document as ``fixed_content``. A clean document may be returned
unchanged — the mandatory part is that the second call is always
performed. When the second pass is unavailable (no coding_tool,
LLM exception, unparseable JSON), ``mandatory=True`` raises
``SelfReviewUnavailableError`` so the generator refuses to promote
an unaudited draft.

These tests pin the contract for the public entry point
``run_doc_self_review`` and the class wrapper ``SelfReviewer``.

Legacy behaviour (silent degradation to the first-pass draft) is
preserved only via the explicit ``mandatory=False`` flag.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _load_module():
    """Import (or re-import) ``backend.self_review`` fresh."""
    if "self_review" in sys.modules:
        del sys.modules["self_review"]
    sys.path.insert(0, str(_BACKEND_DIR))
    try:
        return importlib.import_module("self_review")
    finally:
        if str(_BACKEND_DIR) in sys.path:
            sys.path.remove(str(_BACKEND_DIR))


class TestSelfReviewer:
    """Pin the second-pass LLM contract for ``SelfReviewer``."""

    def test_run_returns_clean_doc_when_already_valid(self):
        """Clean input → LLM is called, the agent judges it clean,
        ``fixed_content`` echoes the original, and the report
        records ``succeeded=True, rewrote=False``.
        """
        sr_mod = _load_module()
        SelfReviewer = sr_mod.SelfReviewer

        content = "# PRD\n\n干净的 PRD 文档，无需修改。"

        class _EchoTool:
            def query(self, prompt, **kwargs):
                return json.dumps(
                    {
                        "findings": [],
                        "fixed_content": content,
                    },
                    ensure_ascii=False,
                )

        tool = _EchoTool()
        reviewer = SelfReviewer(coding_tool=tool)
        report = reviewer.run("prd", content)

        assert isinstance(report, dict)
        assert report.get("doc_type") == "prd"
        assert report.get("succeeded") is True
        assert report.get("rewrote") is False
        assert report.get("fixed_content") == content
        assert report.get("findings") == []

    def test_run_returns_rewrite_when_llm_finds_issues(self):
        """When the LLM flags issues, the report exposes the
        findings *and* the rewritten document via ``fixed_content``.
        The generator decides whether to adopt it.
        """
        sr_mod = _load_module()
        SelfReviewer = sr_mod.SelfReviewer

        rewritten = "# PRD\n\n修订后的 PRD 文档。"

        class _RewriteTool:
            def query(self, prompt, **kwargs):
                return json.dumps(
                    {
                        "findings": [
                            {
                                "severity": "high",
                                "type": "placeholder",
                                "location": "决策点 1",
                                "finding": "包含 TODO 占位符",
                                "action": "删除占位符",
                            }
                        ],
                        "fixed_content": rewritten,
                    },
                    ensure_ascii=False,
                )

        content = "# PRD\n\nTODO: 待补充..."
        reviewer = SelfReviewer(coding_tool=_RewriteTool())
        report = reviewer.run("prd", content)

        assert report.get("succeeded") is True
        assert report.get("rewrote") is True
        assert report.get("fixed_content") == rewritten
        assert report.get("severity_high_count") == 1
        findings = report.get("findings") or []
        assert findings and findings[0].get("type") == "placeholder"

    def test_run_raises_self_review_unavailable_on_llm_failure(self):
        """When the LLM call raises and ``mandatory=True`` (default),
        ``SelfReviewer.run`` MUST propagate
        ``SelfReviewUnavailableError`` so the generator refuses to
        promote the unaudited draft.
        """
        sr_mod = _load_module()
        SelfReviewer = sr_mod.SelfReviewer

        class _BoomTool:
            def query(self, prompt, **kwargs):
                raise RuntimeError("simulated LLM outage")

        reviewer = SelfReviewer(coding_tool=_BoomTool())
        with pytest.raises(sr_mod.SelfReviewUnavailableError):
            reviewer.run("prd", "# PRD\n\n任何内容")

    def test_run_legacy_degrades_when_mandatory_false(self):
        """``mandatory=False`` preserves the historical
        best-effort behaviour: the LLM is still called once,
        the report is returned with ``succeeded=False`` and the
        original draft echoed back via ``fixed_content``.
        """
        sr_mod = _load_module()
        SelfReviewer = sr_mod.SelfReviewer

        class _BoomTool:
            def query(self, prompt, **kwargs):
                raise RuntimeError("simulated LLM outage")

        reviewer = SelfReviewer(coding_tool=_BoomTool())
        content = "# PRD\n\n内容"
        report = reviewer.run(
            "prd", content, mandatory=False,
        )
        assert report.get("succeeded") is False
        assert report.get("attempted") is True
        assert report.get("fixed_content") == content
        assert report.get("error") is not None

    def test_run_parses_fenced_json(self):
        """The parser strips ```json fences and accepts prose
        wrapped JSON replies.
        """
        sr_mod = _load_module()
        SelfReviewer = sr_mod.SelfReviewer

        class _FencedTool:
            def query(self, prompt, **kwargs):
                return (
                    "```json\n"
                    + json.dumps(
                        {
                            "findings": [],
                            "fixed_content": "# PRD\n\n重写",
                        },
                        ensure_ascii=False,
                    )
                    + "\n```"
                )

        reviewer = SelfReviewer(coding_tool=_FencedTool())
        report = reviewer.run("prd", "# PRD\n\n原文")
        assert report.get("succeeded") is True
        assert report.get("rewrote") is True
        assert report.get("fixed_content") == "# PRD\n\n重写"

    def test_empty_content_skips_llm_but_marks_succeeded(self):
        sr_mod = _load_module()
        SelfReviewer = sr_mod.SelfReviewer

        class _NoCallTool:
            def __init__(self):
                self.calls = 0

            def query(self, prompt, **kwargs):
                self.calls += 1
                raise AssertionError(
                    "LLM must NOT be called when input is empty"
                )

        tool = _NoCallTool()
        reviewer = SelfReviewer(coding_tool=tool)
        report = reviewer.run("prd", "")
        assert report.get("succeeded") is True
        assert report.get("fixed_content") == ""
        assert tool.calls == 0

    def test_invalid_doc_type_raises_value_error(self):
        sr_mod = _load_module()
        SelfReviewer = sr_mod.SelfReviewer
        reviewer = SelfReviewer(coding_tool=MagicMock())
        with pytest.raises(ValueError):
            reviewer.run("not-a-real-doc-type", "anything")
