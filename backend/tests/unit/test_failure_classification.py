"""
Contract for :mod:`failure_classification`.

The refiner split gate is "split on the SECOND occurrence of the SAME
failure". Everything else about it -- the extra attempt, the prompt
block, the rules in the refiner's system prompt -- is downstream of one
question this module answers: are these two failures the same failure?

So the tests here are not about parsing convenience. They pin the two
properties the gate is built on:

  * **Stable** -- the same error, twice, must produce the same
    signature. If it did not, the counter would never reach 2, the
    gate would never open, and every task would fail the same way
    this whole change exists to prevent.
  * **Discriminating** -- two different errors must NOT produce the
    same signature. If they did, an unrelated second failure would be
    mistaken for confirmation of the first and would unlock a split on
    no evidence at all.

The third property is what makes the first two usable in production:
real error text is not a constant. It carries the task id, the attempt
number and the blob hash of the run that produced it. A signature that
moved on any of those would be a fresh first occurrence every time.
"""

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from failure_classification import (  # noqa: E402
    EMPTY_DIFF,
    INFRA,
    REPORT_FORMAT,
    TEST_FAILURE,
    UNKNOWN,
    classify_failure,
)


# Real strings, taken from the producers rather than invented, so a
# rename on the producer side shows up here as a broken test instead of
# as a silently-misclassified failure.
AUDIT_REJECTION = (
    "audit_second_pass_failed: evidence table truncated at row 40 of 118; "
    "the reviewer cannot confirm the claimed result"
)
MISMATCH_REJECTION = (
    "Subagent test-report mismatch: claimed TEST_RESULT: PASSED but "
    "pytest exited 3"
)
VACUOUS_REJECTION = (
    "test_command exited 0 but ran no tests (pytest collected 0 items); "
    "cannot certify this task"
)
EMPTY_DIFF_ERROR = (
    "empty_diff_no_changes: task [7] produced no file modifications and "
    "no prior deliverable exists on disk"
)
HARD_TIMEOUT = "HARD TIMEOUT after 1800s while running task 7's test_command"
REAL_TEST_FAILURE = "AssertionError: expected 3, got 2 (tests/test_calc.py::test_sum)"


# ---------------------------------------------------------------------------
# 1. Kind: the split that matters is report_format vs everything else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,expected_kind",
    [
        (AUDIT_REJECTION, REPORT_FORMAT),
        (MISMATCH_REJECTION, REPORT_FORMAT),
        (VACUOUS_REJECTION, REPORT_FORMAT),
        (EMPTY_DIFF_ERROR, EMPTY_DIFF),
        (HARD_TIMEOUT, INFRA),
        (REAL_TEST_FAILURE, TEST_FAILURE),
    ],
)
def test_each_failure_class_maps_to_its_kind(error, expected_kind):
    """One row per rule. The report_format rows are the ones the
    production incident turned on -- a rejected report must never be
    mistaken for a task that is too big.
    """
    assert classify_failure(error).kind == expected_kind


def test_empty_error_is_unknown_not_a_failure():
    """No error text means no claim about the task at all. It must not
    default into ``test_failure``, which would be a split authorised by
    an absence of information.
    """
    assert classify_failure("").kind == UNKNOWN


def test_signature_is_scoped_by_kind():
    """Two errors that normalize identically still must not collide,
    because the kind is part of the signature.
    """
    failure = classify_failure(REAL_TEST_FAILURE)
    assert failure.signature.startswith(f"{TEST_FAILURE}:")


def test_label_is_one_line_and_bounded():
    """The label is rendered into the refiner prompt. A multi-line or
    unbounded error would push the classification block off-screen.
    """
    noisy = "AssertionError: " + ("detail " * 200)
    failure = classify_failure(noisy)
    assert len(failure.label) <= 120
    assert "\n" not in failure.label
    assert failure.label.startswith(f"{TEST_FAILURE}: ")


# ---------------------------------------------------------------------------
# 2. Stability: same error, same signature
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [AUDIT_REJECTION, MISMATCH_REJECTION, EMPTY_DIFF_ERROR, REAL_TEST_FAILURE],
)
def test_the_same_error_twice_yields_the_same_signature(error):
    """The property the whole gate rests on. If this fails, the
    occurrence counter can never pass 1 and the gate never opens.
    """
    first = classify_failure(error)
    second = classify_failure(error)
    assert first.signature == second.signature
    assert first == second


def test_task_id_and_attempt_numbers_do_not_change_the_signature():
    """The same rejection seen on two different tasks / attempts is the
    same rejection. Task ids and attempt counts are the churn this
    normalisation exists to absorb.
    """
    on_task_three = classify_failure(
        "audit_second_pass_failed: task [3] rejected evidence at attempt 2"
    )
    on_task_nineteen = classify_failure(
        "audit_second_pass_failed: task [19] rejected evidence at attempt 7"
    )
    assert on_task_three.signature == on_task_nineteen.signature


# ---------------------------------------------------------------------------
# 3. Discrimination: different error, different signature
# ---------------------------------------------------------------------------


def test_two_different_report_rejections_do_not_collide():
    """The sharp edge of using a readable slug like
    ``report_format:audit-second-pass`` as the key. Both errors here
    are report rejections, and if they shared a signature the second
    one would count as confirmation of the first and open the gate on
    evidence that does not exist.
    """
    missing_rows = classify_failure(
        "audit_second_pass_failed: evidence table truncated at row 12"
    )
    wrong_exit_code = classify_failure(
        "audit_second_pass_failed: exit code quoted as 0 but the run exited 1"
    )
    assert missing_rows.signature != wrong_exit_code.signature


def test_different_kinds_never_share_a_signature():
    """A report rejection and a real test failure are different facts
    even if their text happened to align.
    """
    assert (
        classify_failure(AUDIT_REJECTION).signature
        != classify_failure(REAL_TEST_FAILURE).signature
    )


# ---------------------------------------------------------------------------
# 4. Normalisation: churn must not read as a new failure
# ---------------------------------------------------------------------------


def test_hex_digests_are_masked():
    """Two runs of the same suite produce different blob hashes. Those
    are the same test failure.
    """
    with_hash = classify_failure(f"{REAL_TEST_FAILURE} blob a3f9c1d2e5b7")
    other_hash = classify_failure(f"{REAL_TEST_FAILURE} blob 71bd0ee4aa02")
    assert with_hash.signature == other_hash.signature


def test_numeric_counts_are_masked():
    """1 failed vs 27 failed is the same failure class."""
    one_failed = classify_failure("pytest: 1 failed, 6 passed")
    many_failed = classify_failure("pytest: 27 failed, 118 passed")
    assert one_failed.signature == many_failed.signature


def test_a_digit_only_run_is_a_count_not_a_digest():
    """``12345678`` is also valid hex. Masking it as ``<hex>`` would be
    harmless for stability but would hide that it is a count; both
    masks still normalize, so what is pinned here is that a bare
    number does not stop the two from agreeing.
    """
    assert (
        classify_failure("pytest: 12345678 warnings").signature
        == classify_failure("pytest: 87654321 warnings").signature
    )


def test_case_and_whitespace_do_not_change_the_signature():
    """LLM-generated error text is not case-stable."""
    shouted = classify_failure("ASSERTIONERROR: EXPECTED 3, GOT 2")
    calm = classify_failure("AssertionError:   expected 3,\n got 2")
    assert shouted.signature == calm.signature


def test_substantively_different_text_still_differs_after_normalisation():
    """The counter-check on the normalisation itself: masking must not
    over-merge into a single signature for the whole kind.
    """
    a = classify_failure("pytest: 1 failed in test_alpha")
    b = classify_failure("pytest: 1 failed in test_beta")
    assert a.signature != b.signature
