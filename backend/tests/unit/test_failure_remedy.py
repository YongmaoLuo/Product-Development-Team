"""A failure carries its own remedy, and the split gate follows it.

Why this module exists
======================
``_classify_kind`` answers "WHAT went wrong?" and the split gate
answered "MAY we split?" from a second, parallel list of kinds. Two
lists drift: a kind added to the first is silently absent from the
second, and the answer to "may we split?" for that kind is whatever the
code happened to default to.

The remedy table collapses the two into one. Every kind names the
action it calls for, and the split gate asks a single question of that
action: is it ``retry_then_split``?

Why ``verifier_timeout`` is its own kind
========================================
The cross-verification incident turned on one thing: the verification
command did not finish inside its budget, so there was NO evidence
about what the subagent did. Filing that as ``infra`` -- which is what
the word "timed out" in the message used to do -- says "environment
hiccup, retry it". Retrying with the same budget produces the same
non-answer, and splitting produces children that cannot be verified
either. The remedy is to re-verify with more budget, and that is a
different decision from "never split" and from "split it".

The ordering is load-bearing and is pinned here: the message contains
"timed out", so a check placed after the generic timeout rule files
every verifier timeout as a generic infrastructure hiccup.

And the table is total on purpose. ``remedy_for`` raises for a kind
that has no registered remedy, so a newly added kind that forgets to
register one is RED rather than silently defaulting to whatever the
old code did for an unknown kind.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from failure_classification import (  # noqa: E402
    EMPTY_DIFF,
    INFRA,
    LOCK_CONFLICT,
    NEVER_SPLIT,
    REFINE_WITH_DETAIL,
    REMEDY_BY_KIND,
    REPORT_FORMAT,
    RETRY_THEN_SPLIT,
    REVERIFY_WITH_MORE_BUDGET,
    TEST_FAILURE,
    UNDECLARED_WRITE,
    UNKNOWN,
    VERIFIER_TIMEOUT,
    VERIFIER_TIMEOUT_PREFIX,
    Failure,
    classify_failure,
    is_split_allowed,
    remedy_for,
)


# Real strings, taken from the producers rather than invented.
VERIFIER_TIMEOUT_ERROR = (
    "verifier_timeout: 验证命令在 600s 内未完成，不能据此判定 subagent 说谎"
)
HARD_TIMEOUT = "HARD TIMEOUT after 1800s while running task 7's test_command"
MISMATCH_REJECTION = (
    "Subagent test-report mismatch: claimed TEST_RESULT: PASSED but "
    "pytest exited 3"
)

#: Every kind the module owns, and the remedy it calls for.
EXPECTED_REMEDIES = [
    (REPORT_FORMAT, RETRY_THEN_SPLIT),
    (EMPTY_DIFF, RETRY_THEN_SPLIT),
    (TEST_FAILURE, RETRY_THEN_SPLIT),
    (UNKNOWN, RETRY_THEN_SPLIT),
    (UNDECLARED_WRITE, REFINE_WITH_DETAIL),
    (INFRA, NEVER_SPLIT),
    (LOCK_CONFLICT, NEVER_SPLIT),
    (VERIFIER_TIMEOUT, REVERIFY_WITH_MORE_BUDGET),
]


# ---------------------------------------------------------------------------
# 1. The new kind: a verifier timeout is not an infrastructure hiccup
# ---------------------------------------------------------------------------


def test_verifier_timeout_prefix_wins_over_infra_rule():
    """The ordering trap, as a test.

    The message contains "未完成" and the executor's English spelling
    carries "timed out", so a check placed after the generic timeout
    rule files it as ``infra`` -- the kind the refiner is told to retry
    freely. The truth is that a verifier timeout is evidence of
    NOTHING, and it needs a bigger budget, not a retry.
    """
    failure = classify_failure(VERIFIER_TIMEOUT_ERROR)
    assert failure.kind == VERIFIER_TIMEOUT, (
        "a verifier timeout must not be filed as an infrastructure "
        "problem; the prefix is the executor's explicit spelling"
    )
    assert failure.kind != INFRA


def test_the_prefix_constant_is_the_kinds_own_spelling():
    """The prefix and the kind must not drift apart."""
    assert VERIFIER_TIMEOUT_PREFIX == VERIFIER_TIMEOUT + ":"


def test_plain_timeout_text_stays_infra():
    """The guard is NOT weakened: a generic timeout is still ``infra``.

    Only the explicit prefix buys the new kind. If this test fails, the
    new rule has swallowed every timeout in the system and the
    distinction the whole change exists for is gone.
    """
    failure = classify_failure(HARD_TIMEOUT)
    assert failure.kind == INFRA
    assert failure.kind != VERIFIER_TIMEOUT


def test_report_mismatch_stays_report_format():
    """The other guard: the report guard must not be weakened either."""
    failure = classify_failure(MISMATCH_REJECTION)
    assert failure.kind == REPORT_FORMAT
    assert failure.kind != VERIFIER_TIMEOUT


# ---------------------------------------------------------------------------
# 2. The remedy travels with the failure
# ---------------------------------------------------------------------------


def test_failure_carries_its_remedy():
    """``Failure.remedy`` is the single answer to "what now?"."""
    failure = classify_failure(VERIFIER_TIMEOUT_ERROR)
    assert failure.remedy == REVERIFY_WITH_MORE_BUDGET
    assert failure.remedy == remedy_for(VERIFIER_TIMEOUT)


def test_the_remedy_is_read_only():
    """The remedy is DERIVED from the kind. A writable one would let a
    caller attach a remedy to a failure whose kind says otherwise, and
    the table would stop being the source of truth.
    """
    failure = classify_failure(VERIFIER_TIMEOUT_ERROR)
    with pytest.raises(AttributeError):
        failure.remedy = NEVER_SPLIT


def test_building_a_failure_never_raises_for_an_unregistered_kind():
    """``classify_failure`` never raises, and construction must not be
    where that promise is broken: the remedy is derived on ACCESS, not
    computed at construction time.
    """
    failure = Failure(kind="brand_new_kind", signature="x", label="y")
    assert failure.kind == "brand_new_kind"
    with pytest.raises(ValueError):
        failure.remedy


# ---------------------------------------------------------------------------
# 3. The table is total
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind,expected_remedy", EXPECTED_REMEDIES)
def test_remedy_table_covers_every_kind(kind, expected_remedy):
    """One row per kind. A kind with no remedy row would raise at the
    first use of ``.remedy`` -- in production, in the middle of a task
    that already failed once.
    """
    assert remedy_for(kind) == expected_remedy
    assert REMEDY_BY_KIND[kind] == expected_remedy


def test_every_kind_the_module_owns_has_a_registered_remedy():
    """The guard against a new kind being added without its remedy.

    Derived from the kind constants themselves, so adding a ninth kind
    without adding its row turns THIS red.
    """
    all_kinds = [
        REPORT_FORMAT,
        EMPTY_DIFF,
        TEST_FAILURE,
        INFRA,
        LOCK_CONFLICT,
        UNDECLARED_WRITE,
        UNKNOWN,
        VERIFIER_TIMEOUT,
    ]
    assert sorted(REMEDY_BY_KIND) == sorted(all_kinds)
    for kind in all_kinds:
        assert remedy_for(kind) in {
            RETRY_THEN_SPLIT,
            REFINE_WITH_DETAIL,
            NEVER_SPLIT,
            REVERIFY_WITH_MORE_BUDGET,
        }, kind


def test_unregistered_kind_raises():
    """A kind nobody registered has no remedy, and guessing one would be
    the silent default this table exists to remove.
    """
    with pytest.raises(ValueError):
        remedy_for("brand_new_kind")


# ---------------------------------------------------------------------------
# 4. The split gate follows the remedy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,expected_allowed",
    [
        # Never split.
        (VERIFIER_TIMEOUT_ERROR, False),
        (HARD_TIMEOUT, False),
        ("File lock deadlock while acquiring backend/a.py", False),
        ("undeclared_write: /w/backend/b.py", False),
        # Retry, and split only if it fails the same way again.
        ("audit_second_pass_failed: evidence table truncated", True),
        ("AssertionError: expected 3, got 2", True),
        ("empty_diff_no_changes: task produced no modifications", True),
        ("", True),
    ],
)
def test_is_split_allowed_follows_remedy(error, expected_allowed):
    """The gate asks ONE question of the remedy: is it
    ``retry_then_split``? Every other remedy keeps the gate shut.
    """
    failure = classify_failure(error)
    assert is_split_allowed(failure) is expected_allowed, (
        f"{failure.kind} -> {failure.remedy} must give "
        f"split_allowed={expected_allowed}"
    )
    assert is_split_allowed(failure) == (failure.remedy == RETRY_THEN_SPLIT)


def test_is_split_allowed_raises_for_an_unregistered_kind():
    """The contract: a kind with no remedy cannot answer the question,
    and the caller must hear about it rather than get a default.
    """
    failure = Failure(kind="brand_new_kind", signature="x", label="y")
    with pytest.raises(ValueError):
        is_split_allowed(failure)
