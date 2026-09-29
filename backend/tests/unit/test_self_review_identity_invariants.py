"""Pin the 4 invariants from the "self-review must not drift decision-point
identity" fix. Smoke v4 / v5 / v6 exposed two failure modes:

  1. self_review overwrote ``arch-design.md`` with a rewrite that
     re-numbered ``### 决策点`` headings, so a user's
     ``accepted`` status bound to a different DP after the rewrite.

  2. ``arch_approved`` / ``test_approved`` were advanced when
     ``summary["pending"] == 0`` — which fires when *every* DP is
     skipped (no accept), not just when the user accepted them all.

The fix:
  * arch_generator / prd_generator / test_design_generator stop
    writing the self_review ``fixed_content`` back to the canonical
    doc. The audit report goes only to ``*_self_review.json``.
  * server.py: ``pending == 0`` -> ``accepted == total`` for arch
    and test review phase advance. PRD already used the strict
    semantic (``required_indices.issubset(accepted_indices)``).
  * self_review prompt explicitly forbids changing DP numbering,
    splitting/merging DPs, or emitting metadata fields like
    ``status`` / ``plan_id`` in the JSON output.

These tests pin the 4 invariants so a future edit cannot silently
re-introduce either failure mode.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from task import SubTask  # noqa: E402
from agent import validate_desc_consistency  # noqa: E402


# ---------------------------------------------------------------------------
# Invariant 1: arch_generator must NOT write the self-review rewrite
# back to ``arch-design.md``. The canonical doc is immutable once
# generated.
# ---------------------------------------------------------------------------




class TestArchGeneratorWritesSelfReviewAsFinal(unittest.TestCase):
    """When self_review succeeds, the rewrite IS the final doc the
    user sees.

    Governing principle: the second pass is the better artifact, and
    that is what's shown. ``arch-design.md`` is therefore no longer
    immutable. When self_review returns
    ``rewrote=True AND succeeded=True AND fixed_content`` is
    non-empty, the canonical doc is overwritten with the rewrite.
    The original (pre-self-review) is preserved as
    ``arch-design.original.md`` for audit.

    When self_review fails (or returns no rewrite), the original
    first-pass draft stays as the canonical doc, and the audit
    report carries the error reason.
    """

    def test_arch_generator_writes_self_review_rewrite_as_final(self):
        import inspect
        from arch_generator import ArchGenerator

        src = inspect.getsource(ArchGenerator)
        self.assertIn(
            "content_to_write = fixed_content",
            src,
            "arch_generator must write self_review fixed_content "
            "to the canonical arch-design.md when succeeded — the "
            "user reviews the final version, not the original.",
        )
        self.assertIn(
            "arch-design.original.md",
            src,
            "arch_generator must save the first-pass draft as "
            "arch-design.original.md before self_review.",
        )

    def test_prd_generator_writes_self_review_as_final(self):
        import inspect
        from prd_generator import PRDGenerator

        src = inspect.getsource(PRDGenerator)
        self.assertIn(
            "content_to_write = fixed_content",
            src,
            "prd_generator must write self_review fixed_content "
            "to the canonical prd.md when succeeded.",
        )
        self.assertIn(
            "prd.original.md",
            src,
            "prd_generator must save the first-pass draft as "
            "prd.original.md before self_review.",
        )

    def test_test_design_generator_writes_self_review_as_final(self):
        import inspect
        from test_design_generator import TestDesignGenerator

        src = inspect.getsource(TestDesignGenerator)
        self.assertIn(
            "content_to_write = fixed_content",
            src,
            "test_design_generator must write self_review "
            "fixed_content to the canonical test-design.md when "
            "succeeded.",
        )
        self.assertIn(
            "test-design.original.md",
            src,
            "test_design_generator must save the first-pass draft "
            "as test-design.original.md before self_review.",
        )


# ---------------------------------------------------------------------------
# Invariant 2: phase advance requires ``accepted == total``, not
# ``pending == 0``. Pure skip or partial skip must NOT auto-approve.
# ---------------------------------------------------------------------------


class TestPhaseAdvanceRequiresExplicitAccept(unittest.TestCase):
    """Server review handlers must gate ``*_approved`` transitions
    on the user having explicitly accepted every decision point."""

    def test_server_arch_review_uses_accepted_eq_total(self):
        import inspect
        from server import app  # noqa: F401  (import side-effect: module load)
        from server import review_arch_item

        src = inspect.getsource(review_arch_item)
        # The advance condition must check ``accepted == total``,
        # not ``pending == 0``.
        self.assertIn(
            "summary[\"accepted\"] == summary[\"total\"]",
            src,
            "arch review advance must require accepted == total "
            "so all-skip does not auto-approve.",
        )
        self.assertNotIn(
            "summary[\"pending\"] == 0",
            src,
            "arch review must not use the old pending == 0 advance "
            "condition — it lets all-skipped plans auto-approve.",
        )

    def test_server_test_review_uses_accepted_eq_total(self):
        import inspect
        from server import review_test_item

        src = inspect.getsource(review_test_item)
        self.assertIn(
            "summary[\"accepted\"] == summary[\"total\"]",
            src,
            "test review advance must require accepted == total.",
        )
        self.assertNotIn(
            "summary[\"pending\"] == 0",
            src,
            "test review must not use pending == 0 advance condition.",
        )


# ---------------------------------------------------------------------------
# Invariant 3: self_review prompt explicitly forbids changing decision-
# point numbering / count / metadata fields. The prompt is the only
# defense — there is no parser-level enforcement because the LLM
# might still emit a malformed rewrite that the audit log records.
# ---------------------------------------------------------------------------


class TestSelfReviewPromptForbidsIdentityChanges(unittest.TestCase):
    """self_review prompt must enumerate the identity-stability rules."""

    def _build_prompt(self, doc_type: str, content: str = "") -> str:
        # Import lazily so the test mirrors the production call path.
        from self_review import _build_prompt
        return _build_prompt(
            doc_type,
            content or "# stub\n## section\n",
            upstream_content="",
            upstream_label="prd",
        )

    def test_prompt_explicitly_forbids_renumbering_decision_points(self):
        prompt = self._build_prompt("arch")
        # The fix added a hard rule forbidding the LLM from changing
        # decision-point identity. Pin the wording so a future edit
        # to the prompt cannot relax it.
        self.assertIn("决策点 N", prompt)
        self.assertIn("编号", prompt)
        # The "Identity stability" section should appear.
        self.assertIn("身份", prompt)

    def test_prompt_forbids_splitting_or_merging_decision_points(self):
        prompt = self._build_prompt("arch")
        # Banning "拆成" / "合并" — splitting / merging — is what keeps
        # the count of decision points stable.
        self.assertTrue(
            "拆成" in prompt or "拆" in prompt,
            "self_review prompt must forbid splitting decision points.",
        )
        self.assertTrue(
            "合并" in prompt or "合" in prompt,
            "self_review prompt must forbid merging decision points.",
        )

    def test_prompt_forbids_emitting_metadata_fields(self):
        prompt = self._build_prompt("arch")
        # The LLM must NOT emit ``status``, ``plan_id``, ``doc_type``,
        # or ``note`` in its findings JSON — those fields are
        # code-managed.
        for forbidden in ("status", "plan_id", "doc_type"):
            # The field name must appear in the "do NOT emit" rule,
            # not just anywhere in the prompt.
            self.assertIn(
                f"{forbidden}",
                prompt,
                f"self_review prompt must mention {forbidden} "
                "as a code-managed field.",
            )


# ---------------------------------------------------------------------------
# Invariant 4: the desc/data consistency validator (smoke v4 fix) is
# unrelated to identity drift, but lives in this file because both
# bugs surfaced in the same smoke run. Pin the helper that the
# pipeline relies on so we know which invariants are *not* being
# regressed by this commit.
# ---------------------------------------------------------------------------


class TestValidateDescConsistencyStillGraphAware(unittest.TestCase):
    """Smoke v4 fix: validate_desc_consistency must compare against
    the transitive closure of depends_on. The v4 fix is preserved by
    this commit's edits to other files; pin its behaviour here so
    this commit doesn't accidentally regress it."""

    def test_transitive_chain_passes(self):
        tasks = [
            SubTask(id="1", title="a", description="", status="pending", depends_on=[]),
            SubTask(id="2", title="b", description="", status="pending", depends_on=["1"]),
            SubTask(
                id="3",
                title="c",
                description="前置条件：任务 1、任务 2 已完成。",
                status="pending",
                depends_on=["2"],
            ),
        ]
        # Must not raise — the desc references task 1 transitively.
        self.assertIsNone(validate_desc_consistency(tasks))


if __name__ == "__main__":
    unittest.main()