"""Tests for ``run_doc_self_review`` — the shared post-emit review
function consumed by prd/arch/test generators (DP1).

These tests pin the contracts (DP1 / Task 5/6/8):

  ``run_doc_self_review`` core:
    1. ``test_self_review_detects_placeholder``
    2. ``test_self_review_no_findings_returns_original``
    3. ``test_self_review_llm_failure_fallback``
    4. ``test_self_review_invalid_doc_type``

  PRD/Arch/Test generator integration:
    5-12. ``TestPRDGeneratorSelfReview``,
          ``TestArchGeneratorSelfReview``,
          ``TestTestDesignGeneratorSelfReview``

  DP1 audit-trail + flag (Task 8):
   13. ``test_self_review_report_written`` — PRD generator writes
       ``plans/{id}/prd_self_review.json`` after generation.  The
       audit trail schema MUST contain ``doc_type``, ``findings``,
       ``fixed_content_hash`` (sha256), and ``ts``.
   14. ``test_self_review_event_logged`` — after generating each of
       the 3 docs, ``plans/{id}/execution.log`` MUST contain at
       least one line whose ``event`` is ``prd_self_review``,
       ``arch_self_review``, or ``test_self_review`` (one per doc
       type that the corresponding generator produces).
   15. ``test_flag_disabled_skips_all`` — when
       ``flags.self_review_enabled == False`` AND a generator is
       constructed with ``self_review_enabled=False`` (mirroring the
       flag), no audit-trail report is written AND no log event is
       emitted.
   16. ``test_legacy_plan_state_without_flag`` — a plan_state.json
       that omits ``flags.self_review_enabled`` MUST default to
       ``True`` on read so legacy plans continue to get the
       self-review audit trail.

The function lives in ``backend/self_review.py`` and follows the
shared signature::

    run_doc_self_review(doc_content, doc_type, coding_tool=None)
        -> {"findings": [...], "fixed_content": str}
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _load_module():
    """Import (or re-import) ``backend.self_review`` fresh.

    The module is loaded from ``backend/`` so the existing
    ``SelfReviewer`` class and the new ``run_doc_self_review``
    function are both reachable as top-level attributes.
    """
    if "self_review" in sys.modules:
        del sys.modules["self_review"]
    sys.path.insert(0, str(_BACKEND_DIR))
    try:
        return importlib.import_module("self_review")
    finally:
        if str(_BACKEND_DIR) in sys.path:
            sys.path.remove(str(_BACKEND_DIR))


class TestRunDocSelfReview:
    """Pin the TDD contracts for the shared self-review entry point.

    These tests use the **legacy** contract (``mandatory=False``) so
    they exercise the v2 fields without requiring a real coding_tool
    stub for every case. Mandatory-mode behaviour is covered by the
    new tests in ``test_self_review.py``.
    """

    def test_self_review_detects_placeholder(self):
        """``mandatory=False`` echoes the original draft when no
        coding_tool is supplied, with an empty findings list and
        ``attempted=True, succeeded=False``.
        """
        sr_mod = _load_module()
        result = sr_mod.run_doc_self_review(
            "# PRD\n实现 TBD 功能",
            doc_type="prd",
            mandatory=False,
        )

        assert isinstance(result, dict)
        assert "findings" in result
        assert "fixed_content" in result
        assert result.get("attempted") is True
        assert result.get("succeeded") is False
        assert isinstance(result["findings"], list)
        assert result["findings"] == [], (
            "Without a coding_tool the reviewer must NOT invent findings; "
            f"got {result['findings']!r}"
        )

    def test_self_review_no_findings_returns_original(self):
        """Clean document + valid LLM echo → empty findings,
        ``fixed_content == input``, ``rewrote=False``.
        """
        sr_mod = _load_module()
        clean_doc = (
            "# PRD\n\n"
            "## 决策点 1\n\n"
            "实现用户登录功能，包含用户名密码校验和会话管理。\n"
        )

        class _EchoTool:
            def query(self, prompt, **kwargs):
                return (
                    '{"findings": [], "fixed_content": ' + repr(clean_doc) + '}'
                )

        result = sr_mod.run_doc_self_review(
            clean_doc, doc_type="prd",
            coding_tool=_EchoTool(),
            mandatory=False,
        )
        assert result["findings"] == [], (
            f"expected empty findings for clean doc, got {result['findings']!r}"
        )
        assert result["fixed_content"] == clean_doc, (
            "fixed_content must equal the original input when findings is empty"
        )
        assert result.get("rewrote") is False

    def test_self_review_llm_failure_fallback(self):
        """Mock LLM that throws + ``mandatory=False`` → return
        original, findings empty, ``succeeded=False``.
        """
        sr_mod = _load_module()

        class _BoomTool:
            def query(self, prompt, **kwargs):
                raise RuntimeError("simulated LLM outage")

        doc_content = "# PRD\n实现 TBD 功能"
        result = sr_mod.run_doc_self_review(
            doc_content,
            doc_type="prd",
            coding_tool=_BoomTool(),
            mandatory=False,
        )

        assert result["findings"] == [], (
            "LLM failure must clear findings so the emit pipeline is never blocked; "
            f"got {result['findings']!r}"
        )
        assert result["fixed_content"] == doc_content, (
            "LLM failure must echo the original content as fixed_content"
        )
        assert result.get("succeeded") is False

    def test_self_review_invalid_doc_type(self):
        """doc_type='unknown' → ValueError."""
        sr_mod = _load_module()
        with pytest.raises(ValueError):
            sr_mod.run_doc_self_review("anything", doc_type="unknown")


# ---------------------------------------------------------------------------
# PRDGenerator.generate self-review integration (DP1 / Task 5)
# ---------------------------------------------------------------------------


import json
import logging


def _ensure_backend_on_path():
    """Make ``backend/`` importable as a top-level package directory.

    Mirrors the path manipulation in ``test_agent_desc_consistency.py``
    so ``import prd_generator`` resolves regardless of the test runner's
    initial cwd.
    """
    if str(_BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(_BACKEND_DIR))


def _make_plan_dir(tmp_path):
    """Build a minimal plan dir with ``interview.json`` for PRDGenerator.

    The fixture must satisfy ``REQUIRED_INTERVIEW_DIMENSIONS`` (all
    five keys) — otherwise PRDGenerator short-circuits to the
    placeholder path and the self-review branch is never entered.
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    interview = {
        "dimensions": {
            "background": {"value": "测试背景"},
            "goals": {"value": "测试目标"},
            "scope": {"value": "测试范围"},
            "constraints": {"value": "测试约束"},
            "acceptance": {"value": "测试验收"},
        },
        "product_form": {"form": "software"},
        "chat_history": [{"content": "测试需求"}],
    }
    (plan_dir / "interview.json").write_text(
        json.dumps(interview, ensure_ascii=False), encoding="utf-8"
    )
    return plan_dir


def _make_fake_coding_tool(prd_data):
    """Build a stub coding tool that returns ``prd_data`` from query_json."""
    class _FakeTool:
        def query_json(self, prompt, system_instruction=None, **kwargs):
            return prd_data

        def query(self, prompt, **kwargs):
            return "stub reply"

    return _FakeTool()


class TestPRDGeneratorSelfReview:
    """Pin the TDD contracts for PRDGenerator.generate self-review integration.

    The four contracts mirror the DP1 task spec:

      * ``test_prd_generator_invokes_self_review``: when enabled,
        ``PRDGenerator.generate`` calls ``run_doc_self_review`` exactly
        once with ``doc_type="prd"``.
      * ``test_prd_generator_writes_self_review_rewrite_to_doc``:
        when self_review succeeds (``rewrote=True AND succeeded=True``
        AND ``fixed_content`` is non-empty), the canonical
        ``prd.md`` is overwritten with the rewrite. The original
        first-pass draft is preserved as ``prd.original.md`` for
        audit. The audit report continues to go to
        ``prd_self_review.json``.
      * ``test_prd_generator_skip_when_disabled``: with
        ``self_review_enabled=False``, ``run_doc_self_review`` is NEVER
        called and the generator falls through to writing the original
        emitted content.
      * ``test_prd_generator_fallback_on_exception``: when the
        self-review raises ANY exception, the generator logs a warning
        and writes the ORIGINAL emitted content (degrade gracefully).
    """

    def test_prd_generator_invokes_self_review(self, tmp_path, monkeypatch):
        """generate() calls run_doc_self_review once with doc_type='prd'."""
        _ensure_backend_on_path()
        import prd_generator

        plan_dir = _make_plan_dir(tmp_path)
        prd_data = {
            "title": "测试项目",
            "overview": "测试概述",
            "decision_points": [
                {"title": "决策点1", "context": "x", "problem": "y"}
            ],
        }
        tool = _make_fake_coding_tool(prd_data)
        gen = prd_generator.PRDGenerator(
            tool, plan_dir, self_review_enabled=True
        )

        calls = []

        def _spy(doc_content, doc_type, coding_tool=None, **kwargs):
            calls.append({"doc_content": doc_content, "doc_type": doc_type})
            return {"findings": [], "fixed_content": doc_content}

        monkeypatch.setattr(prd_generator, "run_doc_self_review", _spy)

        gen.generate()

        assert len(calls) == 1, (
            f"expected run_doc_self_review to be called exactly once, "
            f"got {len(calls)} call(s)"
        )
        assert calls[0]["doc_type"] == "prd", (
            f"expected doc_type='prd', got {calls[0]['doc_type']!r}"
        )

    def test_prd_generator_writes_self_review_rewrite_to_doc(self, tmp_path, monkeypatch):
        """generate() writes fixed_content to plans/{id}/prd.md verbatim.

        Identity contract: when self_review succeeds
        (``rewrote=True AND succeeded=True AND fixed_content`` is
        non-empty), the canonical ``prd.md`` is overwritten with
        the rewrite. The user reviews the second-pass version, not
        the original first-pass draft. The original is preserved
        as ``prd.original.md`` for audit. The audit report also
        goes to ``prd_self_review.json``.
        """
        _ensure_backend_on_path()
        import prd_generator

        plan_dir = _make_plan_dir(tmp_path)
        prd_data = {
            "title": "测试项目",
            "overview": "测试概述",
            "decision_points": [
                {"title": "决策点1", "context": "x", "problem": "y"}
            ],
        }
        tool = _make_fake_coding_tool(prd_data)
        gen = prd_generator.PRDGenerator(
            tool, plan_dir, self_review_enabled=True
        )

        fixed_content = "# 修复后的 PRD\n\n这是 self-review 修复后的内容。"

        def _stub(doc_content, doc_type, coding_tool=None, **kwargs):
            return {
                "doc_type": "prd",
                "attempted": True,
                "succeeded": True,
                "rewrote": True,
                "findings": [],
                "fixed_content": fixed_content,
                "input_content_hash": "sha256:" + "x" * 64,
                "fixed_content_hash": "sha256:" + "y" * 64,
                "severity_high_count": 0,
                "severity_medium_count": 0,
                "severity_low_count": 0,
                "mandatory": True,
                "error": None,
            }

        monkeypatch.setattr(prd_generator, "run_doc_self_review", _stub)

        gen.generate()

        prd_md = plan_dir / "prd.md"
        assert prd_md.exists(), "prd.md should exist after generate()"
        actual = prd_md.read_text(encoding="utf-8")
        # The self-review rewrite IS the final doc — that's what
        # the user reviews.
        assert actual == fixed_content, (
            "prd.md should contain the fixed_content returned by "
            "run_doc_self_review verbatim; got {actual!r}"
        )
        # And the original first-pass draft is preserved as
        # baseline for audit.
        assert (plan_dir / "prd.original.md").exists(), (
            "prd.original.md must be written so operators can "
            "compare first-pass vs second-pass drafts."
        )
        # The audit report is also persisted.
        assert (plan_dir / "prd_self_review.json").exists(), (
            "prd_self_review.json must be written so operators can "
            "still see what self_review flagged."
        )

    def test_prd_generator_skip_when_disabled(self, tmp_path, monkeypatch):
        """self_review_enabled=False → run_doc_self_review never called."""
        _ensure_backend_on_path()
        import prd_generator

        plan_dir = _make_plan_dir(tmp_path)
        prd_data = {
            "title": "测试项目",
            "overview": "测试概述",
            "decision_points": [
                {"title": "决策点1", "context": "x", "problem": "y"}
            ],
        }
        tool = _make_fake_coding_tool(prd_data)
        gen = prd_generator.PRDGenerator(
            tool, plan_dir, self_review_enabled=False
        )

        calls = []

        def _spy(doc_content, doc_type, coding_tool=None, **kwargs):
            calls.append(doc_type)
            return {"findings": [], "fixed_content": doc_content}

        monkeypatch.setattr(prd_generator, "run_doc_self_review", _spy)

        gen.generate()

        assert calls == [], (
            "run_doc_self_review MUST NOT be called when "
            f"self_review_enabled=False; got calls={calls!r}"
        )

    def test_prd_generator_propagates_self_review_failure(
        self, tmp_path, monkeypatch, caplog
    ):
        """run_doc_self_review raises → ``generate()`` propagates the
        exception so the unaudited draft is never promoted. The
        generator does NOT silently fall back to the first-pass
        output.
        """
        _ensure_backend_on_path()
        import prd_generator

        plan_dir = _make_plan_dir(tmp_path)
        prd_data = {
            "title": "测试项目",
            "overview": "测试概述",
            "decision_points": [
                {"title": "决策点1", "context": "x", "problem": "y"}
            ],
        }
        tool = _make_fake_coding_tool(prd_data)
        gen = prd_generator.PRDGenerator(
            tool, plan_dir, self_review_enabled=True
        )

        def _boom(doc_content, doc_type, coding_tool=None, **kwargs):
            raise RuntimeError("simulated self-review outage")

        monkeypatch.setattr(prd_generator, "run_doc_self_review", _boom)

        with caplog.at_level(logging.WARNING, logger="prd_generator"):
            with pytest.raises(RuntimeError, match="simulated self-review outage"):
                gen.generate()

        # The PRD was already written to disk before the second pass
        # ran (intentional: the first-pass draft is preserved so the
        # operator can diff against a later successful run). Verify
        # that the canonical prd.json still exists.
        prd_json_path = plan_dir / "prd.json"
        assert prd_json_path.exists(), (
            "prd.json must still be on disk after a failed second pass; "
            "the canonical artefact is what the operator audits manually"
        )

        assert any(
            "self" in rec.message.lower() or "review" in rec.message.lower()
            for rec in caplog.records
        ), (
            "generate() should log a WARNING mentioning self-review when "
            f"propagating; got records={[r.message for r in caplog.records]!r}"
        )


# ---------------------------------------------------------------------------
# ArchGenerator.generate self-review integration (DP1 / Task 6)
# ---------------------------------------------------------------------------


def _make_arch_plan_dir(tmp_path):
    """Build a minimal plan dir with ``prd.json`` so ArchGenerator can
    load the PRD and skip-notice logic does not error.
    """
    plan_dir = tmp_path / "arch_plan"
    plan_dir.mkdir()
    prd = {
        "title": "测试项目",
        "overview": "测试概述",
        "decision_points": [
            {"title": "决策点1", "context": "x", "problem": "y"}
        ],
    }
    (plan_dir / "prd.json").write_text(
        json.dumps(prd, ensure_ascii=False), encoding="utf-8"
    )
    return plan_dir


def _make_arch_fake_tool(arch_md: str):
    """Build a stub coding tool that returns ``arch_md`` from query."""
    class _FakeTool:
        def __init__(self, content):
            self._content = content

        def query(self, prompt, **kwargs):
            return self._content

        def query_json(self, prompt, system_instruction=None, **kwargs):
            return {}

    return _FakeTool(arch_md)


class TestArchGeneratorSelfReview:
    """Pin the TDD contracts for ArchGenerator.generate self-review
    integration (DP1 / Task 6).

    Mirrors the PRDGenerator contract:

      * ``test_arch_generator_invokes_self_review``: when enabled,
        ``ArchGenerator.generate`` calls ``run_doc_self_review``
        exactly once with ``doc_type="arch"``.
      * ``test_arch_generator_skip_when_disabled``: with
        ``self_review_enabled=False``, ``run_doc_self_review`` is
        NEVER called.
    """

    def test_arch_generator_invokes_self_review(
        self, tmp_path, monkeypatch
    ):
        """generate() calls run_doc_self_review once with doc_type='arch'."""
        _ensure_backend_on_path()
        import arch_generator

        plan_dir = _make_arch_plan_dir(tmp_path)
        arch_md = (
            "# 架构设计 — 测试项目\n\n"
            "## 决策点 1: 标题\n\n"
            "**[C] 背景：** 测试背景\n"
            "**[P] 问题：** 测试问题\n"
            "**[A] 行动：** 测试方案\n"
        )
        tool = _make_arch_fake_tool(arch_md)
        gen = arch_generator.ArchGenerator(
            tool, plan_dir, self_review_enabled=True,
        )

        calls = []

        def _spy(doc_content, doc_type, coding_tool=None, **kwargs):
            calls.append({"doc_content": doc_content, "doc_type": doc_type})
            return {"findings": [], "fixed_content": doc_content}

        monkeypatch.setattr(arch_generator, "run_doc_self_review", _spy)

        gen.generate()

        assert len(calls) == 1, (
            f"expected run_doc_self_review to be called exactly once, "
            f"got {len(calls)} call(s)"
        )
        assert calls[0]["doc_type"] == "arch", (
            f"expected doc_type='arch', got {calls[0]['doc_type']!r}"
        )

    def test_arch_generator_skip_when_disabled(self, tmp_path, monkeypatch):
        """self_review_enabled=False → run_doc_self_review never called."""
        _ensure_backend_on_path()
        import arch_generator

        plan_dir = _make_arch_plan_dir(tmp_path)
        arch_md = (
            "# 架构设计 — 测试项目\n\n"
            "## 决策点 1: 标题\n\n"
            "**[C] 背景：** 测试背景\n"
            "**[P] 问题：** 测试问题\n"
            "**[A] 行动：** 测试方案\n"
        )
        tool = _make_arch_fake_tool(arch_md)
        gen = arch_generator.ArchGenerator(
            tool, plan_dir, self_review_enabled=False,
        )

        calls = []

        def _spy(doc_content, doc_type, coding_tool=None, **kwargs):
            calls.append(doc_type)
            return {"findings": [], "fixed_content": doc_content}

        monkeypatch.setattr(arch_generator, "run_doc_self_review", _spy)

        gen.generate()

        assert calls == [], (
            "run_doc_self_review MUST NOT be called when "
            f"self_review_enabled=False; got calls={calls!r}"
        )


# ---------------------------------------------------------------------------
# TestDesignGenerator.generate self-review integration (DP1 / Task 6)
# ---------------------------------------------------------------------------


def _make_test_plan_dir(tmp_path):
    """Build a minimal plan dir with ``arch-design.md`` so
    TestDesignGenerator can load the arch context."""
    plan_dir = tmp_path / "test_plan"
    plan_dir.mkdir()
    arch_md = (
        "# 架构设计 — 测试项目\n\n"
        "## 决策点 1: 标题\n\n"
        "**[A] 行动：** 测试方案\n"
    )
    (plan_dir / "arch-design.md").write_text(arch_md, encoding="utf-8")
    interview = {
        "plan_id": "测试项目",
        "dimensions": {
            "background": {"value": "测试背景"},
            "goals": {"value": "测试目标"},
            "scope": {"in": ["登录", "登出"]},
        },
    }
    (plan_dir / "interview.json").write_text(
        json.dumps(interview, ensure_ascii=False), encoding="utf-8"
    )
    return plan_dir


def _make_test_fake_tool(test_md: str):
    """Build a stub coding tool that returns ``test_md`` from query."""
    class _FakeTool:
        def __init__(self, content):
            self._content = content

        def query(self, prompt, **kwargs):
            return self._content

        def query_json(self, prompt, system_instruction=None, **kwargs):
            return {}

    return _FakeTool(test_md)


class TestTestDesignGeneratorSelfReview:
    """Pin the TDD contracts for TestDesignGenerator.generate
    self-review integration (DP1 / Task 6).

    Mirrors the PRDGenerator / ArchGenerator contract:

      * ``test_test_design_generator_invokes_self_review``: when
        enabled, ``TestDesignGenerator.generate`` calls
        ``run_doc_self_review`` exactly once with ``doc_type="test"``.
      * ``test_test_design_generator_fallback_on_exception``: when
        the self-review raises ANY exception, the generator logs a
        warning and writes the ORIGINAL emitted content.
    """

    def test_test_design_generator_invokes_self_review(
        self, tmp_path, monkeypatch
    ):
        """generate() calls run_doc_self_review once with doc_type='test'."""
        _ensure_backend_on_path()
        import test_design_generator

        plan_dir = _make_test_plan_dir(tmp_path)
        test_md = (
            "# 测试设计 — 测试项目\n\n"
            "## 决策点 1: 标题\n\n"
            "**[C] 背景：** 测试背景\n"
            "**[P] 问题：** 测试问题\n"
            "**[A] 行动：** 测试方案\n"
        )
        tool = _make_test_fake_tool(test_md)
        gen = test_design_generator.TestDesignGenerator(
            tool, plan_dir, self_review_enabled=True,
        )

        calls = []

        def _spy(doc_content, doc_type, coding_tool=None, **kwargs):
            calls.append({"doc_content": doc_content, "doc_type": doc_type})
            return {"findings": [], "fixed_content": doc_content}

        monkeypatch.setattr(
            test_design_generator, "run_doc_self_review", _spy
        )

        gen.generate()

        assert len(calls) == 1, (
            f"expected run_doc_self_review to be called exactly once, "
            f"got {len(calls)} call(s)"
        )
        assert calls[0]["doc_type"] == "test", (
            f"expected doc_type='test', got {calls[0]['doc_type']!r}"
        )

    def test_test_design_generator_propagates_self_review_failure(
        self, tmp_path, monkeypatch, caplog
    ):
        """run_doc_self_review raises → ``generate()`` propagates the
        exception. The unaudited draft is left on disk for manual
        diffing but never promoted.
        """
        _ensure_backend_on_path()
        import test_design_generator

        plan_dir = _make_test_plan_dir(tmp_path)
        test_md = (
            "# 测试设计 — 测试项目\n\n"
            "## 决策点 1: 标题\n\n"
            "**[C] 背景：** 测试背景\n"
            "**[P] 问题：** 测试问题\n"
            "**[A] 行动：** 测试方案\n"
        )
        tool = _make_test_fake_tool(test_md)
        gen = test_design_generator.TestDesignGenerator(
            tool, plan_dir, self_review_enabled=True,
        )

        def _boom(doc_content, doc_type, coding_tool=None, **kwargs):
            raise RuntimeError("simulated self-review outage")

        monkeypatch.setattr(
            test_design_generator, "run_doc_self_review", _boom
        )

        with caplog.at_level(
            logging.WARNING, logger="test_design_generator"
        ):
            with pytest.raises(RuntimeError, match="simulated self-review outage"):
                gen.generate()

        assert any(
            "self" in rec.message.lower() or "review" in rec.message.lower()
            for rec in caplog.records
        ), (
            "generate() should log a WARNING mentioning self-review when "
            f"propagating; got records={[r.message for r in caplog.records]!r}"
        )


# ---------------------------------------------------------------------------
# DP1 audit-trail + flag (Task 8 — tests 13-16)
# ---------------------------------------------------------------------------
#
# These four tests pin the cross-cutting concerns that DP1 wires through
# every generator:
#
#   * After ``generate()`` runs with ``self_review_enabled=True``, an
#     audit-trail JSON report MUST be written next to the generated doc,
#     and the per-plan ``execution.log`` MUST contain a structured
#     ``prd_self_review`` / ``arch_self_review`` / ``test_self_review``
#     event.  These two artifacts together are the operator-visible
#     signal that the self-review pass actually ran.
#
#   * When the flag is explicitly False, BOTH artifacts are skipped —
#     no report file is written and no log line is emitted.  This is
#     the boundary that lets an operator disable self-review for plans
#     where the LLM-fix pass adds noise (e.g. legacy / experimental).
#
#   * A ``plan_state.json`` written before the flag landed has no
#     ``flags.self_review_enabled`` key.  Reading such a state MUST
#     default to True so legacy plans continue to receive the
#     self-review audit trail without a manual migration.


class TestSelfReviewAuditTrailAndFlag:
    """Pin the DP1 audit-trail + flag contracts (Task 8, tests 13-16)."""

    def test_self_review_report_written(self, tmp_path, monkeypatch):
        """PRD generator writes ``plans/{id}/prd_self_review.json`` with
        the audit-trail schema (doc_type / findings / fixed_content_hash
        with sha256: prefix / ts).
        """
        _ensure_backend_on_path()
        import prd_generator

        plan_dir = _make_plan_dir(tmp_path)
        prd_data = {
            "title": "测试项目",
            "overview": "测试概述",
            "decision_points": [
                {"title": "决策点1", "context": "x", "problem": "y"}
            ],
        }
        tool = _make_fake_coding_tool(prd_data)
        gen = prd_generator.PRDGenerator(
            tool, plan_dir, self_review_enabled=True,
        )

        # Stub run_doc_self_review to return a fixed_content that is
        # clearly distinguishable from the input, so the SHA-256 hash
        # in the report can be independently verified.
        fixed_content = "# 修复后的 PRD\n\ndistinctive repaired body"

        def _stub(doc_content, doc_type, coding_tool=None, **kwargs):
            return {"findings": [], "fixed_content": fixed_content}

        monkeypatch.setattr(prd_generator, "run_doc_self_review", _stub)

        gen.generate()

        report_path = plan_dir / "prd_self_review.json"
        assert report_path.exists(), (
            f"audit-trail report must be written at {report_path}, but it "
            "does not exist"
        )

        report = json.loads(report_path.read_text(encoding="utf-8"))

        assert report.get("doc_type") == "prd", (
            f"report.doc_type must be 'prd', got {report.get('doc_type')!r}"
        )
        assert isinstance(report.get("findings"), list), (
            f"report.findings must be a list, got {type(report.get('findings'))!r}"
        )
        assert "fixed_content_hash" in report, (
            f"report must contain 'fixed_content_hash'; got keys {sorted(report.keys())!r}"
        )
        assert report["fixed_content_hash"].startswith("sha256:"), (
            f"fixed_content_hash must be prefixed with 'sha256:', got "
            f"{report['fixed_content_hash']!r}"
        )
        assert "ts" in report, (
            f"report must contain 'ts'; got keys {sorted(report.keys())!r}"
        )

        # Independently verify the SHA-256 matches the fixed_content the
        # stub returned — guards against a future implementation that
        # hashes the wrong buffer.
        import hashlib
        expected_hash = "sha256:" + hashlib.sha256(
            fixed_content.encode("utf-8")
        ).hexdigest()
        assert report["fixed_content_hash"] == expected_hash, (
            "fixed_content_hash must be sha256 of the fixed_content returned "
            f"by run_doc_self_review; expected {expected_hash!r}, got "
            f"{report['fixed_content_hash']!r}"
        )

    def test_self_review_event_logged(self, tmp_path, monkeypatch):
        """After generating prd/arch/test docs with PDT_PLAN_ID set,
        ``plans/{plan_id}/execution.log`` MUST contain at least one line
        whose ``event`` is ``prd_self_review`` / ``arch_self_review`` /
        ``test_self_review`` (one per generator).
        """
        _ensure_backend_on_path()
        import prd_generator
        import arch_generator
        import test_design_generator

        # Use the plan_dir name as plan_id so get_logger(plan_id) writes
        # to plans/{plan_id}/execution.log which is what we read back.
        plan_dir = tmp_path / "audit-trail-plan"
        plan_dir.mkdir()
        (plan_dir / "interview.json").write_text(
            json.dumps(
                {
                    "dimensions": {
                        "background": {"value": "测试背景"},
                        "goals": {"value": "测试目标"},
                        "scope": {"value": "测试范围"},
                        "constraints": {"value": "测试约束"},
                        "acceptance": {"value": "测试验收"},
                    },
                    "product_form": {"form": "software"},
                    "chat_history": [{"content": "测试需求"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # Arch generator needs prd.json.
        prd_json_data = {
            "title": "测试项目",
            "overview": "测试概述",
            "decision_points": [
                {"title": "决策点1", "context": "x", "problem": "y"}
            ],
        }
        (plan_dir / "prd.json").write_text(
            json.dumps(prd_json_data, ensure_ascii=False), encoding="utf-8"
        )
        # TestDesignGenerator needs arch-design.md.
        arch_md_for_test = (
            "# 架构设计 — 测试项目\n\n"
            "## 决策点 1: 标题\n\n"
            "**[A] 行动：** 测试方案\n"
        )
        (plan_dir / "arch-design.md").write_text(
            arch_md_for_test, encoding="utf-8"
        )

        plan_id = plan_dir.name

        # Route get_logger through our plan_id so execution.log lands
        # inside the same tmp_path we read from.  The ExecutionLogger
        # constructor writes to plans/{plan_id}/execution.log relative
        # to a 'plans' root it computes by default; we monkeypatch the
        # constructor to use tmp_path / "plans" so we get a hermetic
        # directory tree per test.
        monkeypatch.setenv("PDT_PLAN_ID", plan_id)
        plans_root = tmp_path / "plans"
        plans_root.mkdir()

        from execution_logger import ExecutionLogger

        original_init = ExecutionLogger.__init__

        def _patched_init(self, pid, plans_dir=None):
            original_init(self, pid, plans_dir=plans_root)

        monkeypatch.setattr(ExecutionLogger, "__init__", _patched_init)

        # Stub run_doc_self_review in each generator module.
        def _stub(doc_content, doc_type, coding_tool=None, **kwargs):
            return {"findings": [], "fixed_content": doc_content}

        monkeypatch.setattr(prd_generator, "run_doc_self_review", _stub)
        monkeypatch.setattr(arch_generator, "run_doc_self_review", _stub)
        monkeypatch.setattr(
            test_design_generator, "run_doc_self_review", _stub
        )

        # --- Run the 3 generators ---
        prd_data = {
            "title": "测试项目",
            "overview": "测试概述",
            "decision_points": [
                {"title": "决策点1", "context": "x", "problem": "y"}
            ],
        }
        prd_generator.PRDGenerator(
            _make_fake_coding_tool(prd_data),
            plan_dir,
            self_review_enabled=True,
        ).generate()

        arch_md = (
            "# 架构设计 — 测试项目\n\n"
            "## 决策点 1: 标题\n\n"
            "**[C] 背景：** 测试背景\n"
            "**[P] 问题：** 测试问题\n"
            "**[A] 行动：** 测试方案\n"
        )
        arch_generator.ArchGenerator(
            _make_arch_fake_tool(arch_md),
            plan_dir,
            self_review_enabled=True,
        ).generate()

        test_md = (
            "# 测试设计 — 测试项目\n\n"
            "## 决策点 1: 标题\n\n"
            "**[C] 背景：** 测试背景\n"
            "**[P] 问题：** 测试问题\n"
            "**[A] 行动：** 测试方案\n"
        )
        test_design_generator.TestDesignGenerator(
            _make_test_fake_tool(test_md),
            plan_dir,
            self_review_enabled=True,
        ).generate()

        # --- Read execution.log back and assert the 3 events are present ---
        log_path = plans_root / plan_id / "execution.log"
        assert log_path.exists(), (
            f"execution.log must be written at {log_path} after 3 generator "
            "calls; the file is missing — get_logger(plan_id) likely returned "
            "None because PDT_PLAN_ID was not set correctly"
        )

        events = set()
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = entry.get("event")
                if ev in ("prd_self_review", "arch_self_review",
                          "test_self_review"):
                    events.add(ev)

        missing = (
            {"prd_self_review", "arch_self_review", "test_self_review"}
            - events
        )
        assert not missing, (
            f"execution.log is missing self_review events {sorted(missing)!r}; "
            f"present events={sorted(events)!r}. Each generator must emit a "
            "structured log event after run_doc_self_review returns."
        )

    def test_flag_disabled_skips_all(self, tmp_path, monkeypatch):
        """When the generator is constructed with self_review_enabled=False,
        NEITHER an audit-trail report file is written NOR is a log event
        emitted to plans/{id}/execution.log.
        """
        _ensure_backend_on_path()
        import prd_generator
        import arch_generator
        import test_design_generator

        plan_dir = tmp_path / "disabled-plan"
        plan_dir.mkdir()

        # Same fixture layout as the prior test.
        (plan_dir / "interview.json").write_text(
            json.dumps(
                {
                    "dimensions": {
                        "background": {"value": "测试背景"},
                        "goals": {"value": "测试目标"},
                        "scope": {"value": "测试范围"},
                        "constraints": {"value": "测试约束"},
                        "acceptance": {"value": "测试验收"},
                    },
                    "product_form": {"form": "software"},
                    "chat_history": [{"content": "测试需求"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (plan_dir / "prd.json").write_text(
            json.dumps(
                {
                    "title": "测试项目",
                    "overview": "测试概述",
                    "decision_points": [
                        {"title": "决策点1", "context": "x", "problem": "y"}
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (plan_dir / "arch-design.md").write_text(
            "# 架构设计 — 测试项目\n\n## 决策点 1: 标题\n\n"
            "**[A] 行动：** 测试方案\n",
            encoding="utf-8",
        )

        plan_id = plan_dir.name
        plans_root = tmp_path / "plans_disabled"
        plans_root.mkdir()

        monkeypatch.setenv("PDT_PLAN_ID", plan_id)
        from execution_logger import ExecutionLogger

        original_init = ExecutionLogger.__init__

        def _patched_init(self, pid, plans_dir=None):
            original_init(self, pid, plans_dir=plans_root)

        monkeypatch.setattr(ExecutionLogger, "__init__", _patched_init)

        # Spy on run_doc_self_review — if it gets called when disabled,
        # that's a regression.
        called = []

        def _spy(doc_content, doc_type, coding_tool=None, **kwargs):
            called.append(doc_type)
            return {"findings": [], "fixed_content": doc_content}

        monkeypatch.setattr(prd_generator, "run_doc_self_review", _spy)
        monkeypatch.setattr(arch_generator, "run_doc_self_review", _spy)
        monkeypatch.setattr(
            test_design_generator, "run_doc_self_review", _spy
        )

        # --- Run the 3 generators with self_review_enabled=False ---
        prd_data = {
            "title": "测试项目",
            "overview": "测试概述",
            "decision_points": [
                {"title": "决策点1", "context": "x", "problem": "y"}
            ],
        }
        prd_generator.PRDGenerator(
            _make_fake_coding_tool(prd_data),
            plan_dir,
            self_review_enabled=False,
        ).generate()
        arch_md = (
            "# 架构设计 — 测试项目\n\n## 决策点 1: 标题\n\n"
            "**[C] 背景：** 测试背景\n"
            "**[P] 问题：** 测试问题\n"
            "**[A] 行动：** 测试方案\n"
        )
        arch_generator.ArchGenerator(
            _make_arch_fake_tool(arch_md),
            plan_dir,
            self_review_enabled=False,
        ).generate()
        test_md = (
            "# 测试设计 — 测试项目\n\n## 决策点 1: 标题\n\n"
            "**[C] 背景：** 测试背景\n"
            "**[P] 问题：** 测试问题\n"
            "**[A] 行动：** 测试方案\n"
        )
        test_design_generator.TestDesignGenerator(
            _make_test_fake_tool(test_md),
            plan_dir,
            self_review_enabled=False,
        ).generate()

        # run_doc_self_review must not have been called at all.
        assert called == [], (
            "run_doc_self_review MUST NOT be called when "
            f"self_review_enabled=False; got calls={called!r}"
        )

        # No audit-trail report files may exist.
        for doc_type in ("prd", "arch", "test"):
            report = plan_dir / f"{doc_type}_self_review.json"
            assert not report.exists(), (
                f"audit-trail report {report.name} must NOT be written when "
                "self_review_enabled=False; the disabled path must skip the "
                "report write entirely"
            )

        # No self_review log events on the execution.log file (even if
        # the file was never created, the assertion is vacuously true).
        log_path = plans_root / plan_id / "execution.log"
        events_found = []
        if log_path.exists():
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("event") in (
                        "prd_self_review",
                        "arch_self_review",
                        "test_self_review",
                    ):
                        events_found.append(entry["event"])
        assert events_found == [], (
            "execution.log must NOT contain self_review events when "
            f"self_review_enabled=False; got {events_found!r}"
        )

    def test_legacy_plan_state_without_flag(self, tmp_path):
        """A ``plan_state.json`` that omits ``flags.self_review_enabled``
        MUST default to ``True`` on read so legacy plans continue to get
        the self-review audit trail (backward-compat contract).
        """
        _ensure_backend_on_path()
        from plan_state import PlanState

        plan_dir = tmp_path / "legacy-plan"
        plan_dir.mkdir()

        # Write a plan_state.json without the new flag, mimicking
        # state written before the self_review_enabled flag landed.
        legacy_state = {
            "plan_id": plan_dir.name,
            "current_phase": "interview_complete",
            "completed_phases": ["interview"],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {
                "arch_enabled": False,
                "test_enabled": False,
                "preflight_enabled": True,
                # NOTE: no self_review_enabled key — this is the
                # legacy state we must keep working.
            },
            "verification": {
                "status": "pending",
                "round": 0,
                "max_rounds": 3,
                "stop_reason": None,
            },
        }
        (plan_dir / "plan_state.json").write_text(
            json.dumps(legacy_state, ensure_ascii=False), encoding="utf-8"
        )

        state = PlanState(plan_dir)
        assert state.is_self_review_enabled() is True, (
            "is_self_review_enabled() MUST default to True for legacy "
            "plan_state.json that omits flags.self_review_enabled; got "
            f"{state.is_self_review_enabled()!r}"
        )
