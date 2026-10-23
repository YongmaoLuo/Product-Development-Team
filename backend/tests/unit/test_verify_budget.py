"""The cross-verify budget has two tiers, and reads the command to pick one.

What this pins
--------------
The cross-verify step runs a task's own ``test_command``, and that
command does not have one runtime magnitude — it has two.  A command
that selects a *directory* of tests imports and runs the whole tree,
which costs minutes and grows with the suite.  A command that names a
single test file imports one module and runs its cases, which costs
seconds.  One number for both is wrong in both directions: the
single-file lane then waits for a tree-sized budget, and the tree-sized
lane is killed by a file-sized budget and reported as a failure — a
slow pass turns into a red one.

So the tier is read off the command, and the number behind each tier is
configuration rather than a constant baked into the caller.  These tests
hold three things in place:

1. **The tier comes from the command.**  A ``tests/`` *directory*
   target is the heavy lane; a single ``.py`` file under ``tests/`` is
   the light lane; a command that does not run pytest at all has no
   pytest magnitude to speak of and takes the light lane.
2. **Both tiers are configurable**, and the environment can override
   either without touching the other.
3. **A budget that is not a usable positive number falls back to the
   default** instead of failing the verification it was meant to size.
   A mistyped budget must never become a zero-second timeout.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import verify_budget  # noqa: E402


# ---------------------------------------------------------------------------
# Which tier a command belongs to
# ---------------------------------------------------------------------------


def test_heavy_lane_for_directory_target():
    """A directory of tests is the full-suite lane."""
    assert verify_budget.is_heavy_lane("pytest tests/unit -q") is True


def test_light_lane_for_single_file_target():
    """One test file is the single-file lane, however deep it sits."""
    assert verify_budget.is_heavy_lane("pytest tests/unit/test_x.py -q") is False


def test_non_pytest_command_is_light():
    """``cargo test`` has no pytest magnitude, so it takes the light tier."""
    assert verify_budget.is_heavy_lane("cargo test") is False


def test_empty_command_is_light():
    """An empty command selects nothing, so nothing is heavy."""
    assert verify_budget.is_heavy_lane("") is False


def test_mixed_directory_and_file_targets_are_heavy():
    """One directory token is enough — the run covers the tree."""
    assert (
        verify_budget.is_heavy_lane("pytest tests/unit tests/unit/test_x.py -q") is True
    )


def test_directory_target_under_a_prefix_is_heavy():
    """The ``tests/`` segment counts wherever it sits in the path."""
    assert verify_budget.is_heavy_lane("cd backend && pytest backend/tests/unit -q") is True


# ---------------------------------------------------------------------------
# The budgets themselves
# ---------------------------------------------------------------------------


def test_defaults_when_env_missing():
    """With nothing configured the ladder is the documented default."""
    assert verify_budget.budget_ladder("pytest tests/unit/test_x.py -q", env={}) == (
        600,
        1800,
        3600,
    )


def test_env_overrides_both_lanes():
    """Each tier has its own variable, and each overrides only its own."""
    env = {
        "PDT_CROSS_VERIFY_TIMEOUT_SEC": "5",
        "PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC": "7",
    }
    assert verify_budget.budget_ladder("pytest tests/unit/test_x.py -q", env=env) == (
        5,
        7,
        14,
    )
    assert verify_budget.budget_ladder("pytest tests/unit -q", env=env) == (7, 7, 14)


def test_invalid_env_falls_back():
    """A value that is not a number falls back to the default."""
    env = {"PDT_CROSS_VERIFY_TIMEOUT_SEC": "abc"}
    assert verify_budget.timeout_seconds("pytest tests/unit/test_x.py -q", env=env) == 600


def test_heavy_lane_ladder():
    """The heavy lane starts at the heavy budget, and escalates from there."""
    assert verify_budget.budget_ladder("pytest tests/unit -q", env={}) == (
        1800,
        1800,
        3600,
    )


def test_unusable_env_values_fall_back():
    """Empty, zero and negative budgets are all unusable."""
    for unusable in ("", "   ", "abc", "0", "-1", "-600"):
        env = {
            "PDT_CROSS_VERIFY_TIMEOUT_SEC": unusable,
            "PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC": unusable,
        }
        assert verify_budget.timeout_seconds(
            "pytest tests/unit/test_x.py -q", env=env
        ) == 600
        assert verify_budget.timeout_seconds("pytest tests/unit -q", env=env) == 1800


def test_env_missing_key_falls_back_to_the_other_tier_default():
    """An unset heavy variable leaves the heavy tier at its own default."""
    env = {"PDT_CROSS_VERIFY_TIMEOUT_SEC": "900"}
    assert verify_budget.timeout_seconds("pytest tests/unit -q", env=env) == 1800
    assert verify_budget.timeout_seconds("pytest tests/unit/test_x.py -q", env=env) == 900


def test_ladder_is_non_decreasing():
    """The rungs are budgets to escalate through, so they never shrink."""
    for command in ("pytest tests/unit -q", "pytest tests/unit/test_x.py -q"):
        first, second, third = verify_budget.budget_ladder(command, env={})
        assert first <= second <= third


def test_env_defaults_to_the_process_environment():
    """``env=None`` reads the process environment."""
    import os

    previous = os.environ.get("PDT_CROSS_VERIFY_TIMEOUT_SEC")
    os.environ["PDT_CROSS_VERIFY_TIMEOUT_SEC"] = "42"
    try:
        assert (
            verify_budget.timeout_seconds("pytest tests/unit/test_x.py -q") == 42
        )
    finally:
        if previous is None:
            os.environ.pop("PDT_CROSS_VERIFY_TIMEOUT_SEC", None)
        else:
            os.environ["PDT_CROSS_VERIFY_TIMEOUT_SEC"] = previous
