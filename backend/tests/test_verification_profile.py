"""
Unit tests for verification_profile.ExecutionProfileGenerator.

The generator projects a static execution profile from a verification
plan: which methods will be exercised, how long each group will take
sequentially, the per-method timeouts, the parallelism cap, and any
sub-task splits that the splitter has already decided to schedule.

These tests pin the contract on three axes:

1. **Field shape** — every required top-level key is present, with the
   expected Python type.
2. **Group partitioning** — VPs are grouped strictly by their
   ``verification_method`` field, in first-seen order.
3. **Total duration** — the sum of per-group durations (each group's
   ``count * timeout``) is the global sequential estimate.

The tests are independent of any LLM, file, or process — the
generator is a pure function over its inputs.
"""

import pytest


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from verification_profile import ExecutionProfileGenerator  # noqa: E402
from verification_config import (  # noqa: E402
    DEFAULT_GLOBAL_TIMEOUT_SECONDS,
    DEFAULT_PARALLELISM_CAP,
    DEFAULT_PER_METHOD_TIMEOUT_SECONDS,
    TimeoutPolicy,
)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _make_plan(verification_points):
    """Wrap a list of VPs in the standard plan envelope."""
    return {"verification_points": verification_points}


# -----------------------------------------------------------------------------
# Field-shape contract
# -----------------------------------------------------------------------------


class TestExecutionProfileFieldShape:
    """The build() output must carry every documented top-level key."""

    def test_execution_profile_field_shape(self):
        """``build()`` returns the full envelope required by the spec.

        Required keys (and types):
        - ``total_duration_sec`` (``int``)
        - ``group_profiles`` (``list``)
        - ``subtask_splits`` (``list``)
        - ``per_method_timeouts`` (``dict``)
        - ``parallelism_cap`` (``int``)
        """
        plan = _make_plan(
            [
                {
                    "id": "VP-001",
                    "verification_method": "automated_test",
                    "expected_result": "passes",
                }
            ]
        )

        gen = ExecutionProfileGenerator(plan)
        profile = gen.build()

        # Top-level keys present
        assert set(profile.keys()) >= {
            "total_duration_sec",
            "group_profiles",
            "subtask_splits",
            "per_method_timeouts",
            "parallelism_cap",
        }

        # Type contract
        assert isinstance(profile["total_duration_sec"], int)
        assert isinstance(profile["group_profiles"], list)
        assert isinstance(profile["subtask_splits"], list)
        assert isinstance(profile["per_method_timeouts"], dict)
        assert isinstance(profile["parallelism_cap"], int)

        # Sanity: at least one group, parallelism is a positive int
        assert len(profile["group_profiles"]) >= 1
        assert profile["parallelism_cap"] > 0

    def test_execution_profile_empty_plan_returns_zero_duration(self):
        """An empty plan should not crash and should report zero duration.

        Important so the orchestrator can render an "empty" state
        without special-casing the missing key.
        """
        gen = ExecutionProfileGenerator(_make_plan([]))
        profile = gen.build()

        assert profile["total_duration_sec"] == 0
        assert profile["subtask_splits"] == []

    def test_execution_profile_none_plan_returns_zero_duration(self):
        """``None`` plan input must be tolerated, not raise."""
        gen = ExecutionProfileGenerator(None)
        profile = gen.build()

        assert profile["total_duration_sec"] == 0
        assert profile["subtask_splits"] == []
        assert profile["parallelism_cap"] == DEFAULT_PARALLELISM_CAP

    def test_execution_profile_uses_timeout_policy_parallelism_cap(self):
        """A custom TimeoutPolicy's parallelism cap is surfaced as-is."""
        custom = TimeoutPolicy(
            per_method_timeout_seconds=dict(DEFAULT_PER_METHOD_TIMEOUT_SECONDS),
            global_default_timeout_seconds=DEFAULT_GLOBAL_TIMEOUT_SECONDS,
            parallelism_cap=8,
        )

        gen = ExecutionProfileGenerator(_make_plan([]), timeout_policy=custom)
        profile = gen.build()

        assert profile["parallelism_cap"] == 8


# -----------------------------------------------------------------------------
# Group partitioning
# -----------------------------------------------------------------------------


class TestExecutionProfileGroupsPartitionByMethod:
    """VPs are bucketed strictly by ``verification_method``."""

    def test_execution_profile_groups_partition_by_method(self):
        """4 ui + 3 cr + 14 auto + 2 api → exactly 4 groups.

        This is the spec example: a mixed-method plan must yield a
        group for every distinct ``verification_method`` value
        (not, e.g. one group per VP, or one group per priority).
        """
        vps = []
        # 4 ui_validation
        for i in range(4):
            vps.append(
                {
                    "id": f"VP-UI-{i}",
                    "verification_method": "ui_validation",
                    "expected_result": f"ui-{i}",
                }
            )
        # 3 code_review
        for i in range(3):
            vps.append(
                {
                    "id": f"VP-CR-{i}",
                    "verification_method": "code_review",
                    "expected_result": f"cr-{i}",
                }
            )
        # 14 automated_test
        for i in range(14):
            vps.append(
                {
                    "id": f"VP-AT-{i}",
                    "verification_method": "automated_test",
                    "expected_result": f"at-{i}",
                }
            )
        # 2 api_test
        for i in range(2):
            vps.append(
                {
                    "id": f"VP-API-{i}",
                    "verification_method": "api_test",
                    "expected_result": f"api-{i}",
                }
            )

        gen = ExecutionProfileGenerator(_make_plan(vps))
        profile = gen.build()

        groups = profile["group_profiles"]
        assert len(groups) == 4

        # Counts match what we put in
        counts_by_method = {g["method"]: g["count"] for g in groups}
        assert counts_by_method == {
            "ui_validation": 4,
            "code_review": 3,
            "automated_test": 14,
            "api_test": 2,
        }

    def test_execution_profile_group_total_duration_equals_method_timeout_times_count(self):
        """Each group's ``duration_seconds`` = ``timeout_seconds * count``."""
        vps = [
            {"id": f"VP-AT-{i}", "verification_method": "automated_test"}
            for i in range(5)
        ]

        gen = ExecutionProfileGenerator(_make_plan(vps))
        profile = gen.build()

        automated_group = next(
            g for g in profile["group_profiles"] if g["method"] == "automated_test"
        )
        assert automated_group["count"] == 5
        # automated_test default = 3600s (2026-09-08: was 120s, raised
        # to 1-hour outer cap), so 5 * 3600 = 18000
        assert automated_group["duration_seconds"] == 3600 * 5
        assert automated_group["timeout_seconds"] == 3600

    def test_execution_profile_total_duration_equals_sum_of_group_durations(self):
        """``total_duration_sec`` = sum(per-group durations)."""
        vps = [
            {"id": "VP-A", "verification_method": "automated_test"},
            {"id": "VP-B", "verification_method": "automated_test"},
            {"id": "VP-C", "verification_method": "ui_validation"},
        ]

        gen = ExecutionProfileGenerator(_make_plan(vps))
        profile = gen.build()

        summed = sum(g["duration_seconds"] for g in profile["group_profiles"])
        assert profile["total_duration_sec"] == summed
        # 2 * 3600 + 1 * 3600 = 7200 + 3600 = 10800
        # 2026-09-08: raised from 120 → 3600 (1-hour outer cap)
        assert profile["total_duration_sec"] == 10800

    def test_execution_profile_per_method_timeouts_includes_used_methods(self):
        """The ``per_method_timeouts`` map must carry every method seen."""
        vps = [
            {"id": "VP-A", "verification_method": "ui_validation"},
            {"id": "VP-B", "verification_method": "automated_test"},
        ]

        gen = ExecutionProfileGenerator(_make_plan(vps))
        profile = gen.build()

        assert profile["per_method_timeouts"]["ui_validation"] == 3600
        assert profile["per_method_timeouts"]["automated_test"] == 3600

    def test_execution_profile_groups_preserve_first_seen_order(self):
        """The first-seen order of methods drives the group list order.

        This is a stability guarantee: callers can rely on
        ``group_profiles[0]`` being the method that appeared first
        in the plan.
        """
        vps = [
            {"id": "VP-A", "verification_method": "api_test"},
            {"id": "VP-B", "verification_method": "automated_test"},
            {"id": "VP-C", "verification_method": "ui_validation"},
            {"id": "VP-D", "verification_method": "automated_test"},
        ]

        gen = ExecutionProfileGenerator(_make_plan(vps))
        profile = gen.build()

        methods = [g["method"] for g in profile["group_profiles"]]
        assert methods == ["api_test", "automated_test", "ui_validation"]


# -----------------------------------------------------------------------------
# Sub-task split surface
# -----------------------------------------------------------------------------


class TestExecutionProfileSubtaskSplits:
    """The profile must surface splits recorded via ``record_subtask_split``."""

    def test_record_subtask_split_appends_to_subtask_splits(self):
        """Recording two splits yields two entries, in insertion order."""
        gen = ExecutionProfileGenerator(_make_plan([]))

        gen.record_subtask_split("VP-001", ["VP-001-1", "VP-001-2", "VP-001-3"])
        gen.record_subtask_split("VP-007", ["VP-007-1", "VP-007-2"])

        profile = gen.build()

        assert profile["subtask_splits"] == [
            {
                "parent_vp_id": "VP-001",
                "sub_vp_ids": ["VP-001-1", "VP-001-2", "VP-001-3"],
            },
            {"parent_vp_id": "VP-007", "sub_vp_ids": ["VP-007-1", "VP-007-2"]},
        ]

    def test_record_subtask_split_with_empty_list_is_a_noop(self):
        """An empty sub-vp list must NOT create a phantom split entry."""
        gen = ExecutionProfileGenerator(_make_plan([]))
        gen.record_subtask_split("VP-001", [])

        assert gen.build()["subtask_splits"] == []

    def test_subtask_splits_default_to_empty_list(self):
        """A fresh generator with no splits recorded has ``subtask_splits == []``."""
        gen = ExecutionProfileGenerator(_make_plan([]))
        assert gen.build()["subtask_splits"] == []