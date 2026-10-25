"""One ``vp_complete`` per VP, carrying the classification fields.

Every verification point has to announce that it finished, exactly once.
The single emission site is :meth:`Orchestrator._run_single_vp_async`;
the payload it writes is built by
:meth:`Orchestrator._vp_complete_payload`, so the shape of the event and
the round it belongs to have exactly one definition rather than one per
call site.

Three fields travel with the event because the consumers downstream of
the event log cannot recompute them. ``failure_class`` is what the loop
counts (``deliverable_defect`` counts; ``criterion_defect`` /
``out_of_scope`` / ``infra`` do not), ``failure_class_source`` says
whether the class was earned or assumed (``"defaulted"`` means assumed),
and ``evidence_quote`` is the fragment of raw output the claim is
anchored in — the thing a human needs before deciding to act on it.

The conservative defaults matter more than the happy path here. A
verdict object that predates these fields — or any other caller that
simply does not carry them — must not silently claim a passed
deliverable is broken, and must not silently claim a counted failure is
some other class either: it lands on ``deliverable_defect`` /
``"defaulted"`` / ``""``, which is the same reading
:class:`Verdict`'s own field defaults give it.

A passed verdict, by contrast, has an *empty* class and that emptiness
is load-bearing: it is the convention that says "no failure to
classify". Defaulting it to ``deliverable_defect`` would report every
green VP as a broken deliverable.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_agent import Orchestrator  # noqa: E402
from verification_subagent import Verdict  # noqa: E402


_PAYLOAD_KEYS = {
    "vp_id",
    "round",
    "status",
    "failure_class",
    "failure_class_source",
    "evidence_quote",
    "actual_result",
}


class _LegacyVerdict:
    """A verdict as it looked before the classification fields existed.

    A plain stand-in rather than a ``Verdict`` because ``Verdict``
    fills the three fields in its own constructor — constructing one
    and deleting the attributes would test the deletion, not the
    fallback. This object genuinely has never heard of them.
    """

    def __init__(self, verdict: str, reasons: list, evidence: list) -> None:
        self.verdict = verdict
        self.reasons = reasons
        self.evidence = evidence


def _failed_verdict() -> Verdict:
    return Verdict(
        verdict="FAILED",
        reasons=["criterion 3 never ran"],
        evidence=["pytest: 1 failed"],
        failure_class="criterion_defect",
        failure_class_source="llm",
        evidence_quote="1 failed, 0 passed",
    )


def test_projection_fields():
    """The three classification fields survive the projection.

    :meth:`Orchestrator._verdict_to_result` collapses the richer
    ``Verdict`` into the legacy result dict the Phase 2 pipeline
    consumes. The classification fields have to make it across that
    collapse — a caller holding only the result dict is otherwise
    unable to tell a counted defect from an infrastructure blip.
    """
    result = Orchestrator._verdict_to_result("VP-007", _failed_verdict())

    assert result["failure_class"] == "criterion_defect"
    assert result["failure_class_source"] == "llm"
    assert result["evidence_quote"] == "1 failed, 0 passed"

    # The legacy keys the projection already carried are untouched —
    # this is additive, not a reshape.
    assert result["id"] == "VP-007"
    assert result["status"] == "FAILED"


def test_legacy_defaults():
    """A verdict without the three fields is read conservatively.

    Absence is not evidence of anything, so the class falls back to the
    one that *counts* rather than to one of the three that would refuse
    to count the attempt; the source is marked ``"defaulted"`` so a
    reader can tell the class was assumed rather than earned; and the
    quote is empty because there is no anchor to quote.
    """
    legacy = _LegacyVerdict(
        verdict="FAILED",
        reasons=["something went wrong"],
        evidence=["stderr: boom"],
    )

    result = Orchestrator._verdict_to_result("VP-003", legacy)

    assert result["failure_class"] == "deliverable_defect"
    assert result["failure_class_source"] == "defaulted"
    assert result["evidence_quote"] == ""

    # Same defaults on the event payload, so the event log cannot
    # disagree with the result dict about what was assumed.
    payload = Orchestrator._vp_complete_payload("VP-003", 1, legacy)
    assert payload["failure_class"] == "deliverable_defect"
    assert payload["failure_class_source"] == "defaulted"
    assert payload["evidence_quote"] == ""


def test_passed_verdict_has_empty_class():
    """A passed verdict's class stays empty.

    ``Verdict.__post_init__`` empties the class for a pass, and that
    empty string is the signal that there is no failure to classify.
    Reading absence as "default it to ``deliverable_defect``" would
    turn every green VP into a reported broken deliverable, so the
    fallback has to key on a *missing attribute*, not on an empty one.
    """
    passed = Verdict(verdict="PASSED", reasons=["all green"], evidence=[])

    result = Orchestrator._verdict_to_result("VP-001", passed)

    assert result["failure_class"] == ""
    assert Orchestrator._vp_complete_payload("VP-001", 2, passed)["failure_class"] == ""


def test_payload_key_set():
    """The event payload carries exactly the contracted keys.

    Exact equality, not a superset check: an extra key that lands in the
    JSONL event log is a field some downstream reader will come to
    depend on, and the contract here is a closed set. A missing key is
    worse — a reader would have to guess.
    """
    payload = Orchestrator._vp_complete_payload("VP-007", 3, _failed_verdict())

    assert set(payload) == _PAYLOAD_KEYS

    assert payload["vp_id"] == "VP-007"
    assert payload["round"] == 3
    assert payload["status"] == "FAILED"
    assert payload["failure_class"] == "criterion_defect"
    assert payload["failure_class_source"] == "llm"
    assert payload["evidence_quote"] == "1 failed, 0 passed"
    assert payload["actual_result"] == "criterion 3 never ran"


def test_single_emission_site():
    """Exactly one ``vp_complete`` emission in the module.

    Two emission sites means two events for one VP, which makes every
    "did this point finish?" question ambiguous downstream: a reader
    counting completions double-counts, and one tracking the last event
    sees whichever branch happened to run last. Pinned as a structural
    assertion so a second call site cannot land unnoticed.
    """
    source = (_BACKEND_DIR / "verification_agent.py").read_text(encoding="utf-8")

    assert source.count('"vp_complete"') == 1