"""
Unit tests for verification_split.SplitDecision.

The split policy is intentionally a pure function with three rules:

1. ``result["status"]`` must be ``"timeout"`` or ``"hard_timeout"``.
2. ``vp["expected_result"]`` must contain at least one ``;``.
3. After stripping and dropping empties, at least two clauses remain.

When all three hold, the VP is decomposed into sub-VPs (one per
clause), each carrying ``parent_vp_id`` and the original
``verification_method`` + ``timeout_seconds``.

These tests are deliberately data-driven: the policy lives or dies on
its branching, not on I/O. There is no fixture-heavy setup.
"""

import pytest


# Make `verification_split` importable when pytest is launched from
# either the project root or the `backend/` directory.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from verification_split import SplitDecision  # noqa: E402


# -----------------------------------------------------------------------------
# Negative cases: do NOT split
# -----------------------------------------------------------------------------


class TestSplitDecisionNoSplit:
    """Cases that must return None (no decomposition)."""

    def test_split_decision_single_clause_returns_none(self):
        """A VP with a single expected_result clause must NOT be split.

        The spec example: a UI validation with a single expectation
        ``"A"`` should not be decomposed, regardless of status, because
        there is nothing meaningful to split along.
        """
        vp = {
            "id": "VP-001",
            "verification_method": "ui_validation",
            "expected_result": "A 通过",
        }
        result = {"id": "VP-001", "status": "timeout"}

        assert SplitDecision.should_split(vp, result) is None

    def test_split_decision_non_timeout_returns_none(self):
        """A non-timeout failure must NOT trigger a split.

        Spec rule: only ``status == "timeout"`` / ``"hard_timeout"``
        are eligible for sub-task decomposition. A ``failed`` status is
        the repair-task generator's responsibility, not the
        splitter's.
        """
        vp = {
            "id": "VP-001",
            "verification_method": "ui_validation",
            "expected_result": "A 通过; B 通过; C 通过",
        }
        result = {"id": "VP-001", "status": "failed"}

        assert SplitDecision.should_split(vp, result) is None

    @pytest.mark.parametrize("status", ["passed", "FAILED", "skipped", ""])
    def test_split_decision_other_statuses_return_none(self, status):
        """Exhaustive negative-status coverage."""
        vp = {
            "id": "VP-001",
            "verification_method": "ui_validation",
            "expected_result": "A 通过; B 通过",
        }
        result = {"id": "VP-001", "status": status}

        assert SplitDecision.should_split(vp, result) is None

    def test_split_decision_hard_timeout_also_splits(self):
        """2026-09-13 bugfix: the HardTimeoutError branch in
        ``_run_single_vp_async`` passes ``status="hard_timeout"`` —
        the splitter must accept it, otherwise the local clause
        splitter is dead code on the hard-timeout path."""
        vp = {
            "id": "VP-023",
            "verification_method": "automated_test",
            "expected_result": "compose up OK; pytest 全过; 枚举测试通过",
        }
        result = {"id": "VP-023", "status": "hard_timeout"}

        chunks = SplitDecision.should_split(vp, result)

        assert chunks is not None
        assert len(chunks) == 3
        assert all(c["timeout_seconds"] == 3600 for c in chunks)

    def test_split_decision_empty_expected_result_returns_none(self):
        """No expected_result → no split (no clauses to chunk)."""
        vp = {"id": "VP-001", "verification_method": "ui_validation"}
        result = {"id": "VP-001", "status": "timeout"}

        assert SplitDecision.should_split(vp, result) is None

    def test_split_decision_empty_clauses_only_returns_none(self):
        """``\";;\"`` or trailing ``;`` yields 0 real clauses → no split."""
        vp = {
            "id": "VP-001",
            "verification_method": "ui_validation",
            "expected_result": ";;",
        }
        result = {"id": "VP-001", "status": "timeout"}

        assert SplitDecision.should_split(vp, result) is None

    def test_split_decision_missing_vp_id_returns_none(self):
        """A VP without an ``id`` cannot produce a valid sub-id → no split.

        Sub-VP ids are derived as ``f"{parent_id}-{index}"``; an empty
        parent id would yield ``\"-1\"`` and create collisions across
        splits, so we refuse rather than risk that.
        """
        vp = {
            "verification_method": "ui_validation",
            "expected_result": "A 通过; B 通过",
        }
        result = {"id": "VP-001", "status": "timeout"}

        assert SplitDecision.should_split(vp, result) is None


# -----------------------------------------------------------------------------
# Positive case: split into N sub-VPs
# -----------------------------------------------------------------------------


class TestSplitDecisionSplit:
    """Cases that must produce a list of sub-VPs."""

    def test_split_decision_timeout_with_multi_clause_returns_chunks(self):
        """The spec example: 3 ``;``-separated clauses → 3 sub-VPs.

        Each sub-VP must:
        - Inherit the original ``verification_method``.
        - Carry ``parent_vp_id`` back to the original VP.
        - Carry the single (stripped) clause in ``expected_result``.
        - Carry the flat ``timeout_seconds`` (3600, 2026-09-13 plan).
        """
        vp = {
            "id": "VP-001",
            "verification_method": "ui_validation",
            "expected_result": "A 通过; B 通过; C 通过",
        }
        result = {"id": "VP-001", "status": "timeout"}

        chunks = SplitDecision.should_split(vp, result)

        assert chunks is not None
        assert len(chunks) == 3
        assert [c["id"] for c in chunks] == ["VP-001-1", "VP-001-2", "VP-001-3"]
        for chunk in chunks:
            assert chunk["parent_vp_id"] == "VP-001"
            assert chunk["verification_method"] == "ui_validation"
            assert chunk["timeout_seconds"] == 3600
        assert [c["expected_result"] for c in chunks] == [
            "A 通过",
            "B 通过",
            "C 通过",
        ]

    def test_split_decision_strips_whitespace_around_clauses(self):
        """Leading/trailing whitespace around clauses must be stripped.

        Real-world ``expected_result`` strings often look like
        ``\"A 通过 ; B 通过 ; C 通过\"`` — extra spaces around the
        separators. The splitter must normalise these.
        """
        vp = {
            "id": "VP-007",
            "verification_method": "automated_test",
            "expected_result": "A 通过 ; B 通过 ; C 通过",
        }
        result = {"id": "VP-007", "status": "timeout"}

        chunks = SplitDecision.should_split(vp, result)

        assert chunks is not None
        assert [c["expected_result"] for c in chunks] == [
            "A 通过",
            "B 通过",
            "C 通过",
        ]

    def test_split_decision_ignores_per_vp_timeout_override(self):
        """2026-09-13: the per-VP ``timeout_seconds`` override was
        DELETED — the splitter must NOT pass a legacy plan value on;
        children always carry the flat 3600."""
        vp = {
            "id": "VP-009",
            "verification_method": "automated_test",
            "expected_result": "X; Y",
            "timeout_seconds": 60,
        }
        result = {"id": "VP-009", "status": "timeout"}

        chunks = SplitDecision.should_split(vp, result)

        assert chunks is not None
        assert all(c["timeout_seconds"] == 3600 for c in chunks)

    def test_split_decision_two_clauses_returns_two_sub_vps(self):
        """Lower bound: two valid clauses → exactly two sub-VPs."""
        vp = {
            "id": "VP-010",
            "verification_method": "code_review",
            "expected_result": "alpha; beta",
        }
        result = {"id": "VP-010", "status": "timeout"}

        chunks = SplitDecision.should_split(vp, result)

        assert chunks is not None
        assert len(chunks) == 2
        assert [c["id"] for c in chunks] == ["VP-010-1", "VP-010-2"]
        assert [c["expected_result"] for c in chunks] == ["alpha", "beta"]