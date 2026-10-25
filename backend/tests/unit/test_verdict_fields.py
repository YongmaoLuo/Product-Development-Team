"""The three fields the failure classification reads off a verdict.

``verification.failure_triage.classify_verdict`` reads three fields off
a verdict record: the class the failure carries, the source that class
came from, and the quote the claim is anchored in. A verdict that does
not carry them cannot be classified, so :class:`Verdict` grew the three
fields — with the conservative defaults, because a verdict nobody
classified is a verdict nobody has disproved either:

  * ``failure_class`` — ``"deliverable_defect"``: the class that
    *counts*. The other three classes either skip the attempt or
    refuse to count it, so an unclassified failure must not quietly
    become one of them.
  * ``failure_class_source`` — ``"defaulted"``: the class was assumed,
    not earned.
  * ``evidence_quote`` — ``""``: there is no anchor yet.

A passed verdict has no failure to classify, so its class is empty —
the same convention ``classify_verdict`` returns for a passed verdict.

The defaults are spelled out as literals here rather than imported from
``verification.failure_triage``: ``verification_subagent`` is imported
by ``verification_agent``, which the ``verification`` package imports in
turn, so a module-level import of that package here would be a circular
import. The drift guard below is what keeps the two in step.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_subagent import (  # noqa: E402
    Verdict,
    parse_verdict_to_dataclass,
)


# ---------------------------------------------------------------------------
# The defaults
# ---------------------------------------------------------------------------


def test_verdict_defaults():
    """A verdict that says nothing about its failure still carries the
    class that counts, and says so — a defaulted class is not the same
    as a missing one."""
    verdict = Verdict(verdict="FAILED", reasons=["x"])

    assert verdict.failure_class == "deliverable_defect"
    assert verdict.failure_class_source == "defaulted"
    assert verdict.evidence_quote == ""


def test_passed_empty_class():
    """A passed verdict has no failure, so it has no class."""
    verdict = Verdict(verdict="PASSED", reasons=[], evidence=[], failure_class="")

    assert verdict.failure_class == ""


def test_a_passed_verdict_without_an_explicit_class_is_empty_too():
    """The backstop: a construction point that never passes the field
    must not end up claiming the deliverable is broken."""
    verdict = Verdict(verdict="PASSED", reasons=[], evidence=[])

    assert verdict.failure_class == ""


def test_the_defaults_match_the_failure_triage_vocabulary():
    """The drift guard. The literals in ``verification_subagent`` and
    the constants in ``verification.failure_triage`` are the same
    values; this is what makes that true rather than a coincidence."""
    from verification import failure_triage

    verdict = Verdict(verdict="FAILED")

    assert verdict.failure_class == failure_triage.DELIVERABLE_DEFECT
    assert verdict.failure_class_source == failure_triage.SOURCE_DEFAULTED


# ---------------------------------------------------------------------------
# The existing fields are untouched
# ---------------------------------------------------------------------------


def test_the_existing_fields_keep_their_order_and_their_defaults():
    """The new fields are appended, so every existing construction
    point — positional or keyword — keeps meaning what it meant."""
    names = [f.name for f in dataclasses.fields(Verdict)]

    assert names[:6] == [
        "verdict",
        "reasons",
        "evidence",
        "provider",
        "chosen_model",
        "model_complexity",
    ]
    assert names[6:] == [
        "failure_class",
        "failure_class_source",
        "evidence_quote",
    ]

    defaults = {
        f.name: f.default
        for f in dataclasses.fields(Verdict)
        if f.default is not dataclasses.MISSING
    }
    assert defaults["provider"] == ""
    assert defaults["chosen_model"] == ""
    assert defaults["model_complexity"] == ""


def test_an_existing_construction_point_still_works():
    """The construction points that predate the three fields do not
    pass them, and must not start raising."""
    verdict = Verdict(
        verdict="FAILED",
        reasons=["coding tool call failed"],
        evidence=[],
        provider="vendor-a-pro",
        chosen_model="Vendor A-M3",
        model_complexity="complex",
    )

    assert verdict.verdict == "FAILED"
    assert verdict.reasons == ["coding tool call failed"]
    assert verdict.failure_class == "deliverable_defect"


def test_a_positional_construction_point_still_means_the_same_thing():
    """Appending the fields must not shift the ones that came first."""
    verdict = Verdict("FAILED", ["x"], ["y"], "p", "m", "complex")

    assert verdict.verdict == "FAILED"
    assert verdict.reasons == ["x"]
    assert verdict.evidence == ["y"]
    assert verdict.provider == "p"
    assert verdict.chosen_model == "m"
    assert verdict.model_complexity == "complex"


# ---------------------------------------------------------------------------
# The parse path
# ---------------------------------------------------------------------------


def test_the_parse_path_gives_a_passed_verdict_an_empty_class():
    """``parse_verdict_to_dataclass`` is the construction point for a
    passed verdict, and a passed verdict has no failure to classify."""
    verdict = parse_verdict_to_dataclass(
        '{"verdict": "PASSED", "reasons": [], "evidence": []}'
    )

    assert verdict.failure_class == ""


def test_the_parse_path_carries_the_claim_through():
    """What the model claimed, and the quote it anchored the claim in,
    reach the verdict — otherwise the fields could never carry anything
    but their defaults."""
    verdict = parse_verdict_to_dataclass(
        '{"verdict": "FAILED", "reasons": [], "evidence": [],'
        ' "failure_class": "infra", "failure_class_source": "llm",'
        ' "evidence_quote": "connection refused"}'
    )

    assert verdict.failure_class == "infra"
    assert verdict.failure_class_source == "llm"
    assert verdict.evidence_quote == "connection refused"


def test_the_parse_path_defaults_a_failed_verdict_without_a_claim():
    """A payload that says nothing about its failure gets the class
    that counts, marked as defaulted."""
    verdict = parse_verdict_to_dataclass(
        '{"verdict": "FAILED", "reasons": [], "evidence": []}'
    )

    assert verdict.failure_class == "deliverable_defect"
    assert verdict.failure_class_source == "defaulted"
    assert verdict.evidence_quote == ""


@pytest.mark.parametrize(
    "payload",
    [
        '{"verdict": "SKIPPED", "reasons": [], "evidence": []}',
        '{"verdict": "FAILED", "reasons": [], "evidence": []}',
    ],
)
def test_a_failed_verdict_is_never_classed_as_anything_but_the_default(
    payload: str,
):
    """``SKIPPED`` is coerced to ``FAILED`` at parse time, and neither
    of them may smuggle in a class the model never earned."""
    verdict = parse_verdict_to_dataclass(payload)

    assert verdict.verdict == "FAILED"
    assert verdict.failure_class == "deliverable_defect"
    assert verdict.failure_class_source == "defaulted"
