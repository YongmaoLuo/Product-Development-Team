"""A failure fingerprint is a function of the FAILURE, not of the prose.

2026-10-29 (VP-030, root-cause report ``root-cause-VP-030.md``).

The chain decides "the root cause moved" by comparing two fingerprints.
The fingerprint was computed from four inputs — the VP id, the failure
class, the failed assertions, the command — and one of them was
``first_error_line``, which the orchestrator *derived* from the report's
``actual_result``: a paragraph of Chinese prose that every round's
report writer reworded.

So the fingerprint tracked the wording.  The same failure payload, with
one evidence line rephrased, produced a different fingerprint, and the
chain read that as "the root cause moved" and dispatched another
diagnosis — three times over (``repair-r2-04`` / ``repair-r3-02`` /
``repair-r1-96``) for one unchanged defect.

The rule pinned here: **when the caller has a payload, the prose cannot
reach the hash.**  Without a payload the prose is still the only thing
there is, so it is still used — but then the fingerprint is explicitly
marked as prose-derived, and nothing downstream may claim it describes
the failure's content.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.failure_fingerprint import (  # noqa: E402
    FINGERPRINT_LENGTH,
    canonical_fingerprint_input,
    failure_fingerprint,
)


_FAILED = [
    "[响应携带被搜索的目录] json_path searched_dir: expected the subject "
    "to exist; it does not",
]


# ---------------------------------------------------------------------------
# 1 — the payload decides
# ---------------------------------------------------------------------------


def test_the_same_payload_with_a_reworded_evidence_line_is_one_failure():
    """The defect, restated as a fingerprint that does not move."""
    first = failure_fingerprint(
        vp_id="VP-030",
        failure_class="deliverable_defect",
        failed_assertions=_FAILED,
        command=None,
        first_error_line="框架记录：status_code=404，断言失败。",
    )
    second = failure_fingerprint(
        vp_id="VP-030",
        failure_class="deliverable_defect",
        failed_assertions=_FAILED,
        command=None,
        first_error_line=(
            "框架记录：status_code=404，assertions_failed 两条均为 json_path "
            "缺 $ 根。"
        ),
    )
    assert first == second


def test_the_canonical_input_does_not_carry_the_prose_when_a_payload_exists():
    one = canonical_fingerprint_input(
        vp_id="VP-030",
        failure_class="deliverable_defect",
        failed_assertions=_FAILED,
        first_error_line="一句话",
    )
    two = canonical_fingerprint_input(
        vp_id="VP-030",
        failure_class="deliverable_defect",
        failed_assertions=_FAILED,
        first_error_line="另一句话",
    )
    assert one == two


def test_a_different_payload_is_a_different_failure():
    """Negative control: the fix must not flatten every failure together."""
    first = failure_fingerprint(
        vp_id="VP-030",
        failure_class="deliverable_defect",
        failed_assertions=_FAILED,
        first_error_line="same prose",
    )
    second = failure_fingerprint(
        vp_id="VP-030",
        failure_class="deliverable_defect",
        failed_assertions=["[另一条断言] expected equals 404, got 500"],
        first_error_line="same prose",
    )
    assert first != second


def test_the_prose_is_still_used_when_there_is_no_payload():
    """No payload ⇒ the prose is all there is.

    It is kept (a fingerprint is better than none), but the caller can
    see that it was prose-derived: the canonical input says so.
    """
    one = canonical_fingerprint_input(
        vp_id="VP-030",
        failure_class="deliverable_defect",
        failed_assertions=None,
        first_error_line="一句话",
    )
    two = canonical_fingerprint_input(
        vp_id="VP-030",
        failure_class="deliverable_defect",
        failed_assertions=None,
        first_error_line="另一句话",
    )
    assert one != two
    assert "first_error_line=" in one


def test_the_fingerprint_is_short_and_stable():
    value = failure_fingerprint(
        vp_id="VP-030",
        failure_class="deliverable_defect",
        failed_assertions=_FAILED,
    )
    assert len(value) == FINGERPRINT_LENGTH
    assert value == failure_fingerprint(
        vp_id="VP-030",
        failure_class="deliverable_defect",
        failed_assertions=_FAILED,
    )


def test_the_order_of_the_failed_assertions_does_not_matter():
    """The assertions are a SET, not a sequence."""
    first = failure_fingerprint(
        vp_id="VP-030",
        failure_class="x",
        failed_assertions=["a failure", "b failure"],
    )
    second = failure_fingerprint(
        vp_id="VP-030",
        failure_class="x",
        failed_assertions=["b failure", "a failure"],
    )
    assert first == second


# 2026-11-07 (merge origin/main): the second half of this file — the
# decision the fingerprint fed ("does this failure need a root-cause task
# instead of a repair?", :func:`needs_root_cause_task`) — is **not** kept:
# the root-cause chain is gone, and the decision belonged to it.  The rule
# the first half pins (a payload decides, the prose never does) is kept:
# the fingerprint is still computed from the payload.
