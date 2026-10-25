"""Tier-B evidence anchoring, the tier-B fallback, and method strength.

Three pure rules, all of them about *not being fooled*:

1. ``validate_evidence_quote(quote, actual_result)`` — a tier-B
   classification is only as good as the fragment of raw output it
   points at, and that fragment has to appear **verbatim** in the
   actual result: same case, same spacing, same characters. A quote
   that matches only after normalising whitespace, case or Unicode
   form proves nothing, because the model that produced it is exactly
   the thing under suspicion. Nine equivalence classes; only two of
   them pass.
2. ``classify_verdict(verdict)`` — the model's class survives only if
   its quote verifies. An unverifiable quote degrades the verdict to
   ``deliverable_defect`` with the source ``defaulted``. The
   deterministic tier needs no anchor: it was decided by a rule, not by
   a model.
3. ``is_not_looser(original, replacement)`` — when a broken criterion
   is replaced, the replacement may not be a weaker check than the one
   it replaces. The methods are ranked by how much assurance their
   pass actually carries, and a method the table does not know is
   incomparable — incomparable means rejected.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


# Ensure backend/ is on sys.path so ``import verification`` works
# regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _triage():
    """The module under test.

    Imported inside the test bodies on purpose: a module that does not
    exist (or does not export the contract) then shows up as a test
    FAILURE carrying the real reason, instead of a collection ERROR
    that hides which contract broke.
    """
    try:
        from verification import failure_triage
    except ImportError as exc:  # pragma: no cover - the RED phase
        pytest.fail(f"verification.failure_triage is not importable: {exc}")
    return failure_triage


# ---------------------------------------------------------------------------
# The nine equivalence classes of "the same quote"
# ---------------------------------------------------------------------------
#
# "Verbatim" is the whole rule, so the failure modes are enumerable:
# identical, a true substring, and the near-misses — the ways of
# *almost* matching. Only the first two pass. The near-misses are the
# interesting half: they are exactly the quotes a model produces when
# it is paraphrasing the output instead of quoting it, and accepting
# them would let an unanchored claim through the gate that exists to
# stop it.
QUOTE_EQUIVALENCE_CLASSES = [
    # (label, quote, actual_result, expected)
    ("identical", "schema rejected id", "schema rejected id", True),
    (
        "true substring",
        "schema rejected",
        "VP-007: schema rejected id",
        True,
    ),
    (
        "leading whitespace",
        " schema rejected id",
        "schema rejected id",
        False,
    ),
    (
        "trailing whitespace",
        "schema rejected id ",
        "schema rejected id",
        False,
    ),
    (
        "internal whitespace (double space)",
        "schema  rejected id",
        "schema rejected id",
        False,
    ),
    (
        "internal whitespace (tab for space)",
        "schema\trejected id",
        "schema rejected id",
        False,
    ),
    (
        "case difference",
        "Schema Rejected Id",
        "schema rejected id",
        False,
    ),
    (
        "unicode normalisation difference (NFC vs NFD)",
        "schema rejecte\u0301d id",
        "schema rejecte\u00e9d id",
        False,
    ),
    ("empty quote", "", "schema rejected id", False),
    ("none quote", None, "schema rejected id", False),
    (
        "one punctuation apart",
        "schema rejected id.",
        "schema rejected id",
        False,
    ),
]


def test_quote_equivalence_classes():
    """A quote is accepted only when it appears verbatim in the result.

    The nine classes are the enumerable ways a quote can relate to the
    text it claims to quote. Two pass: the whole string, and a
    genuine substring of it. Everything else — a quote that needs its
    whitespace collapsed, its case folded or its Unicode form
    normalised to match — is a paraphrase, and a paraphrase is not an
    anchor. Accepting those is what "the model said so" looks like
    from the inside.
    """
    triage = _triage()

    for label, quote, actual_result, expected in QUOTE_EQUIVALENCE_CLASSES:
        assert triage.validate_evidence_quote(quote, actual_result) is expected, (
            f"{label}: quote {quote!r} against {actual_result!r} should "
            f"validate to {expected!r}"
        )

    # The false-negative guard: a short quote is still a real quote.
    # A gate that rejected short anchors would be trivially bypassed by
    # quoting a longer fragment — which is the opposite of what this
    # rule is for.
    assert triage.validate_evidence_quote("abc", "xxabcxx") is True
    assert triage.validate_evidence_quote("id", "VP-007: schema rejected id") is True

    # A missing actual result cannot anchor anything.
    assert triage.validate_evidence_quote("schema rejected", None) is False
    assert triage.validate_evidence_quote("schema rejected", "") is False

    # An all-whitespace quote anchors nothing either: it carries no
    # fragment to check, so it cannot vouch for a classification.
    assert triage.validate_evidence_quote("   ", "VP-007: schema rejected id") is False


# ---------------------------------------------------------------------------
# The tier-B fallback
# ---------------------------------------------------------------------------


@dataclass
class _VerdictLike:
    """A verdict record in attribute form.

    The production record is a dataclass (``Verdict``) in one call
    site and a plain dict in another, so the rule has to read both.
    """

    verdict: str
    failure_class: str = ""
    failure_class_source: str = ""
    evidence_quote: str = ""
    actual_result: str = ""


def test_classify_verdict_fallback():
    """An unverifiable quote degrades the verdict to a deliverable defect.

    Tier B is the model's opinion, and the model's opinion is only
    actionable when it points at something in the raw output. When it
    does not, the verdict is filed as ``deliverable_defect`` with the
    source ``defaulted``: the work gets reworked, the attempt counts,
    and nobody is told the model had a reason. The reason matters
    because the other three classes either skip the attempt or do not
    count it — a degraded verdict must not quietly become one of them.
    """
    triage = _triage()

    # --- the anchor verifies: the model's class survives ------------
    verified = {
        "verdict": "FAILED",
        "failure_class": "criterion_defect",
        "evidence_quote": "schema rejected id",
        "actual_result": "VP-007: schema rejected id",
    }
    assert triage.classify_verdict(verified) == ("criterion_defect", "llm")

    # --- the anchor does not verify: the verdict is degraded -------
    unverifiable = [
        (
            "the quote is not in the actual result",
            {
                "verdict": "FAILED",
                "failure_class": "criterion_defect",
                "evidence_quote": "schema rejected the identifier",
                "actual_result": "VP-007: schema rejected id",
            },
        ),
        (
            "the quote is missing",
            {
                "verdict": "FAILED",
                "failure_class": "criterion_defect",
                "evidence_quote": None,
                "actual_result": "VP-007: schema rejected id",
            },
        ),
        (
            "the quote is empty",
            {
                "verdict": "FAILED",
                "failure_class": "criterion_defect",
                "evidence_quote": "",
                "actual_result": "VP-007: schema rejected id",
            },
        ),
        (
            "the quote is whitespace only",
            {
                "verdict": "FAILED",
                "failure_class": "criterion_defect",
                "evidence_quote": "   ",
                "actual_result": "VP-007: schema rejected id",
            },
        ),
        (
            "the quote matches only after case folding",
            {
                "verdict": "FAILED",
                "failure_class": "criterion_defect",
                "evidence_quote": "Schema Rejected Id",
                "actual_result": "VP-007: schema rejected id",
            },
        ),
        (
            "the actual result is missing",
            {
                "verdict": "FAILED",
                "failure_class": "criterion_defect",
                "evidence_quote": "schema rejected id",
            },
        ),
        (
            "the claimed class is not in the vocabulary",
            {
                "verdict": "FAILED",
                "failure_class": "cosmic_rays",
                "evidence_quote": "schema rejected id",
                "actual_result": "VP-007: schema rejected id",
            },
        ),
        (
            "the claimed class is missing",
            {
                "verdict": "FAILED",
                "failure_class": None,
                "evidence_quote": "schema rejected id",
                "actual_result": "VP-007: schema rejected id",
            },
        ),
    ]

    for label, record in unverifiable:
        assert triage.classify_verdict(record) == (
            "deliverable_defect",
            "defaulted",
        ), f"{label}: should degrade to deliverable_defect/defaulted"

    # --- the deterministic tier needs no anchor ---------------------
    # A watchdog kill was decided by a rule, not by a model: there is
    # no quote to check, and degrading it would file an environment
    # failure as a broken deliverable — the exact confusion the four
    # classes exist to prevent.
    deterministic = {
        "verdict": "FAILED",
        "failure_class": "infra",
        "failure_class_source": "deterministic",
        "evidence_quote": "",
        "actual_result": "watchdog killed the subprocess after 900s",
    }
    assert triage.classify_verdict(deterministic) == ("infra", "deterministic")

    # --- a verdict that passed has no failure to classify -----------
    passed = {
        "verdict": "PASSED",
        "failure_class": "",
        "failure_class_source": "",
        "evidence_quote": "",
        "actual_result": "",
    }
    assert triage.classify_verdict(passed) == ("", "")

    # --- the same record in attribute form reads the same -----------
    assert triage.classify_verdict(
        _VerdictLike(
            verdict="FAILED",
            failure_class="criterion_defect",
            evidence_quote="schema rejected id",
            actual_result="VP-007: schema rejected id",
        )
    ) == ("criterion_defect", "llm")
    assert triage.classify_verdict(
        _VerdictLike(
            verdict="FAILED",
            failure_class="criterion_defect",
            evidence_quote="schema rejected the identifier",
            actual_result="VP-007: schema rejected id",
        )
    ) == ("deliverable_defect", "defaulted")


# ---------------------------------------------------------------------------
# The method strength table
# ---------------------------------------------------------------------------

#: The pairs the table is built from, strongest first. Each entry is
#: (stronger method, weaker method): replacing the stronger one with
#: the weaker one is a loosening and must be refused, while the
#: reverse — an upgrade — is allowed.
STRICTLY_STRONGER = [
    ("full_ci", "e2e"),
    ("e2e", "api_test"),
    ("api_test", "code_review"),
    ("code_review", "ui_validation"),
]


def test_is_not_looser_matrix():
    """A replacement may not be a weaker check than the original.

    The table is a ranking of how much assurance a method's pass
    actually carries, so the whole matrix is decided by it: a
    replacement at least as strong as the original is accepted, a
    weaker one is refused, and the same method is accepted because it
    is not looser than itself.

    A method the table does not carry is incomparable, and
    incomparable means refused. Guessing that an unknown method is
    strong enough is how a replacement quietly becomes weaker than the
    check it replaces — the guard has to fail closed.
    """
    triage = _triage()

    strength = triage.METHOD_STRENGTH
    assert strength, "the strength table must not be empty"

    # --- the whole matrix, decided by the table ---------------------
    for original, original_strength in strength.items():
        for replacement, replacement_strength in strength.items():
            expected = replacement_strength >= original_strength
            assert (
                triage.is_not_looser(original, replacement) is expected
            ), f"replacing {original!r} with {replacement!r} should be " f"{expected}"

    # --- the same method is never looser than itself ----------------
    for method in strength:
        assert triage.is_not_looser(method, method) is True, (
            f"{method!r} replaced by itself should be allowed"
        )

    # --- the ranking the table encodes, pinned independently --------
    # Derived from the table this assertion would be tautological, so
    # the pairs are spelled out: the whole-repo gate outranks a single
    # flow, a real flow outranks a single interface call, an
    # objective check outranks a model reading the code, and a model
    # reading the whole source outranks a model reading one click.
    for stronger, weaker in STRICTLY_STRONGER:
        assert triage.is_not_looser(stronger, weaker) is False, (
            f"replacing {stronger!r} with the weaker {weaker!r} must be "
            f"refused"
        )
        assert triage.is_not_looser(weaker, stronger) is True, (
            f"replacing {weaker!r} with the stronger {stronger!r} must be "
            f"allowed"
        )

    # --- unknown methods are incomparable, hence refused ------------
    for unknown in ("cosmic_rays", "", "api_tests", "manual_check", None):
        assert triage.is_not_looser(unknown, "code_review") is False, (
            f"an unknown original method {unknown!r} must be refused"
        )
        assert triage.is_not_looser("code_review", unknown) is False, (
            f"an unknown replacement method {unknown!r} must be refused"
        )

    # The brief's own example: a code review replaced by a single UI
    # interaction is a loosening, not a tightening.
    assert triage.is_not_looser("code_review", "ui_validation") is False
