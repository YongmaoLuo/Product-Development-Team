"""Normalising the verdict text, and refusing the values we do not know.

What this file is about
-----------------------
The verdict is the one thing a sub-agent says about *itself*, and the
pipeline downstream reads it as a closed vocabulary: ``PASSED``,
``FAILED``, or ``SKIPPED`` (which is coerced to ``FAILED`` because
skipping is not an outcome — see PRD decision point 3).

An LLM that is asked for ``"verdict": "PASSED"`` does not always answer
``"verdict": "PASSED"``. It answers ``"passed"``, ``"Passed"``,
``"  PASSED  "``, ``"PASSED."`` with a trailing sentence, or — quite
often — ``"通过"``. Before this change the parse layer took the text
as it came: anything that was not exactly ``"SKIPPED"`` and not exactly
one of the two canonical spellings was rejected, so ``"passed"`` was
an error and the caller retried; and a value the parse layer *did*
let through was carried verbatim, so a verdict record could hold a
status string the rest of the pipeline had never been told to expect.

Two failure modes, one cause: the text was never put into the
vocabulary before it was used. This file pins the normalisation that
puts it there — strip the surrounding whitespace, fold the case — and
pins the refusal that keeps unrecognised text out:

  * a value that normalises to a known verdict is accepted and
    *returned in canonical form*, so the caller always gets
    ``"PASSED"`` / ``"FAILED"`` and never ``"passed"``;
  * a value that normalises to nothing known is rejected with
    ``VerdictParseError``, exactly like a malformed payload — the
    caller retries rather than filing a verdict the loop cannot
    interpret.

``"通过"`` is rejected on purpose. The verdict vocabulary is English
and closed; accepting a Chinese synonym would mean accepting every
synonym, and the point of a closed vocabulary is that there is
exactly one spelling of each outcome.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_subagent import (  # noqa: E402
    VerdictParseError,
    parse_verdict,
    parse_verdict_to_dataclass,
)


# ---------------------------------------------------------------------------
# Case and surrounding whitespace
# ---------------------------------------------------------------------------


def test_normalize_case():
    """The same verdict, spelled the way a model happens to spell it.

    The returned value is the *canonical* one, not the input: a caller
    that compares the verdict against ``"PASSED"`` must not have to
    normalise it a second time, and a verdict record written from this
    string must not carry ``"passed"`` into the pipeline.
    """
    assert parse_verdict({"verdict": "PASSED"}) == "PASSED"
    assert parse_verdict({"verdict": "passed"}) == "PASSED"
    assert parse_verdict({"verdict": "PaSsEd"}) == "PASSED"
    assert parse_verdict({"verdict": "  passed  "}) == "PASSED"
    assert parse_verdict({"verdict": "\tPASSED\n"}) == "PASSED"

    assert parse_verdict({"verdict": "FAILED"}) == "FAILED"
    assert parse_verdict({"verdict": "failed"}) == "FAILED"
    assert parse_verdict({"verdict": " Failed "}) == "FAILED"


# ---------------------------------------------------------------------------
# Unrecognised text
# ---------------------------------------------------------------------------


def test_unknown_text_raises():
    """A verdict the pipeline has no meaning for is an error, not a
    value to pass on.

    The error is the same one a malformed payload raises, because from
    the caller's point of view they are the same situation: there is
    no verdict here to act on, so the only correct move is to retry.
    """
    for text in ("通过", "MAYBE", "true", "1", "PASS", "PASSED.", "n/a"):
        with pytest.raises(VerdictParseError):
            parse_verdict({"verdict": text})


def test_an_empty_verdict_raises():
    """An empty string is a verdict nobody made — not a pass."""
    with pytest.raises(VerdictParseError):
        parse_verdict({"verdict": ""})


def test_a_whitespace_only_verdict_raises():
    """Whitespace normalises away to nothing, and nothing is not a
    verdict."""
    with pytest.raises(VerdictParseError):
        parse_verdict({"verdict": "   "})


@pytest.mark.parametrize("value", [None, 1, 1.0, True, ["PASSED"], {"v": 1}])
def test_a_non_string_verdict_raises(value):
    """A verdict that is not text at all is malformed, the same way a
    missing verdict is."""
    with pytest.raises(VerdictParseError):
        parse_verdict({"verdict": value})


def test_a_missing_verdict_raises():
    with pytest.raises(VerdictParseError):
        parse_verdict({})


def test_the_error_names_the_value_it_refused():
    """A rejection that does not say what was wrong sends the caller
    hunting for the wrong bug."""
    with pytest.raises(VerdictParseError) as exc:
        parse_verdict({"verdict": "通过"})

    assert "通过" in str(exc.value)


# ---------------------------------------------------------------------------
# SKIPPED still means FAILED
# ---------------------------------------------------------------------------


def test_skipped_to_failed():
    """PRD decision point 3 — zero tolerance for skipping — survives the
    normalisation, and gets the same treatment: a skipped verdict is a
    failed verdict, so the downstream judgment never has to handle a
    third status."""
    assert parse_verdict({"verdict": "SKIPPED"}) == "FAILED"
    assert parse_verdict({"verdict": "skipped"}) == "FAILED"
    assert parse_verdict({"verdict": "  Skipped "}) == "FAILED"


# ---------------------------------------------------------------------------
# The dataclass path
# ---------------------------------------------------------------------------


def test_the_dataclass_path_normalises_too():
    """The dataclass wrapper validates through the same door, so the
    verdict it carries is canonical too."""
    verdict = parse_verdict_to_dataclass(
        '{"verdict": " passed ", "reasons": ["ok"], "evidence": ["out"]}'
    )

    assert verdict.verdict == "PASSED"


def test_the_dataclass_path_refuses_unknown_text():
    with pytest.raises(VerdictParseError):
        parse_verdict_to_dataclass(
            '{"verdict": "通过", "reasons": [], "evidence": []}'
        )


def test_the_dataclass_path_coerces_skipped():
    verdict = parse_verdict_to_dataclass(
        '{"verdict": "skipped", "reasons": [], "evidence": []}'
    )

    assert verdict.verdict == "FAILED"


def test_the_classification_fields_pass_through():
    """What the model claimed, and the quote it anchored the claim in,
    reach the dataclass — the three fields exist to carry something
    other than their defaults."""
    verdict = parse_verdict_to_dataclass(
        '{"verdict": "failed", "reasons": [], "evidence": [],'
        ' "failure_class": "infra", "failure_class_source": "llm",'
        ' "evidence_quote": "connection refused"}'
    )

    assert verdict.verdict == "FAILED"
    assert verdict.failure_class == "infra"
    assert verdict.failure_class_source == "llm"
    assert verdict.evidence_quote == "connection refused"


def test_the_classification_fields_default_when_the_payload_omits_them():
    """A payload that says nothing about its failure is not an error —
    it gets the class that counts, marked as defaulted."""
    verdict = parse_verdict_to_dataclass(
        '{"verdict": "failed", "reasons": [], "evidence": []}'
    )

    assert verdict.failure_class == "deliverable_defect"
    assert verdict.failure_class_source == "defaulted"
    assert verdict.evidence_quote == ""


def test_a_normalised_pass_carries_no_failure_class():
    """A pass has no failure to classify, and the empty class is the
    convention for that case."""
    verdict = parse_verdict_to_dataclass(
        '{"verdict": "passed", "reasons": [], "evidence": []}'
    )

    assert verdict.verdict == "PASSED"
    assert verdict.failure_class == ""
