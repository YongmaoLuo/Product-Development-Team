"""Semantic triage for a verification point that has failed for the same
reason too many times.

Why this module exists
======================

The four-class failure vocabulary (``verification.failure_triage``) is
what the HITL loop acts on: ``infra`` is retried, ``deliverable_defect``
is reworked, ``criterion_defect`` is escalated to a human because no
amount of rework can satisfy a broken check, and ``out_of_scope`` is
skipped. The deterministic tier (tier A) can only recognise a handful of
signals, and it answers ``None`` — "I do not know" — for everything
else. That ``None`` used to be where the decision died: the loop had a
failure, the rules could not name its nature, and there was nowhere to
send it.

This module is the **semantic tier** (tier B): one LLM call that reads
the recorded failure and names its nature. It is deliberately not a
verdict-producing agent. It does not run the test, does not look at the
code, and cannot ask a second question — it reads the failure that was
already recorded and classifies it.

Two properties make the answer safe to act on
=============================================

**One call, no tools.** ``judge()`` makes exactly one LLM call and that
call carries ``allowed_tools=[]``. A judge that could run commands would
investigate instead of classifying — and a second investigation is a
second opinion, which is how a triage starts costing rounds instead of
saving them. A judge that could ask twice would also quietly disagree
with itself, and the loop would have to decide which of the two answers
to believe.

**The answer must be anchored.** A class is only worth acting on when
it points at something, so the answer carries the fragment of raw output
it was based on (``evidence_quote``), and that fragment has to appear
**verbatim** in the recorded failure text. This is the same anchor rule
the verdict path uses
(:func:`verification.failure_triage.validate_evidence_quote`): same
case, same spacing, same characters. A quote that matches only after
normalisation is a paraphrase, and a paraphrase is what a model produces
when it is describing what it saw instead of quoting it.

An answer that cannot be checked is not an answer
=================================================

Everything that could go wrong produces a conclusion that is not
conclusive. But **"not conclusive" is not one thing**, and the caller
has to tell the two apart, because they call for opposite decisions:

* **The triage malfunctioned.** The provider blew up, the payload was
  not a JSON object, or the quote the answer carried is not in the
  recorded failure. None of these says anything about the *failure* —
  they say something about the *triage*. A conclusion like that is
  marked ``available=False``: the caller must not read it as a
  statement about the failure, so the repair path gets its chance
  instead of the loop stopping on a failure nobody ever classified.
* **The triage ran and could not characterise the failure.** The
  provider answered, the answer was readable, and the answer names no
  class the loop can act on (the class is outside the vocabulary, or
  no quote was offered at all). The conclusion is marked
  ``available=True`` and ``conclusive=False``: the triage spoke, and
  what it said is that the failure is unclassifiable — which is
  exactly the case a human has to look at.

The distinction is carried by one field, ``available``, because the
caller cannot recover it from anything else: a malfunctioned triage
and an unclassifiable failure produce the same
``conclusive=False`` conclusion, and stopping on one of them voids a
criterion over a triage that never ran.

Such a conclusion carries the class that counts
(``deliverable_defect``), so the caller's fall-back is the repair path
rather than a silent skip or an unearned escalation. The same
reasoning the verdict path uses
(:func:`verification.failure_triage.classify_verdict`): the other three
classes either skip the attempt or do not count it, so a verdict nobody
could verify must not quietly become one of them.

The invariant that follows: **a non-empty ``evidence_quote`` on a
conclusion is a quote that was verified.** An inconclusive conclusion
carries no quote at all — the rejected one is named in the rationale,
where it cannot be mistaken for an anchor.

What this module deliberately does NOT do
==========================================

It does not decide what to do with the conclusion. Applying a
replacement criterion, checking that the replacement is not a *weaker*
check than the one it replaces
(:func:`verification.failure_triage.is_not_looser`) and moving the loop
forward are the caller's business — this module only produces the
classified, anchored answer, or says plainly that it could not.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from verification.failure_triage import (
    ALL_FAILURE_CLASSES,
    DELIVERABLE_DEFECT,
    validate_evidence_quote,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The vocabulary and the fallback
# ---------------------------------------------------------------------------

#: The class an inconclusive conclusion carries.
#:
#: It is the class that *counts* — the one a human is meant to see and
#: the one the repair path is built for. A conclusion the judge could
#: not check therefore falls back to "the work is wrong, rework it",
#: which is the conservative reading of an unreadable failure.
FALLBACK_CLASS = DELIVERABLE_DEFECT


def _normalize_class(value: Any) -> Optional[str]:
    """The canonical class a claimed name names, or ``None``.

    Dialect tolerance, and nothing else: ``"Criterion Defect"`` and
    ``"criterion-defect"`` name the same class as
    ``"criterion_defect"``, but a name outside the four-class
    vocabulary names no class at all and returns ``None``. The rule
    mirrors :func:`verification.failure_triage.classify_verdict`'s
    treatment of a claim outside the vocabulary — a claim that was never
    earned is degraded, never guessed into existence.

    Args:
        value: The claimed class, in whatever spelling the model used.

    Returns:
        The canonical class name, or ``None`` when the claim is outside
        the vocabulary.
    """
    if value is None:
        return None
    key = re.sub(r"[\s\-]+", "_", str(value).strip().lower()).strip("_")
    if key in ALL_FAILURE_CLASSES:
        return key
    return None


# ---------------------------------------------------------------------------
# The conclusion
# ---------------------------------------------------------------------------


@dataclass
class TriageConclusion:
    """What the judge concluded about one verification point.

    Seven fields, and every one of them is load-bearing:

    ``vp_id``
        The point the conclusion is about. A conclusion nobody can
        attach to a point is a conclusion nobody can act on.
    ``failure_class``
        One of the four classes, or — for an inconclusive conclusion —
        :data:`FALLBACK_CLASS`.
    ``conclusive``
        Whether the answer may be acted on. ``False`` means the judge
        could not check it, and the caller falls back to the repair
        path.
    ``evidence_quote``
        The fragment of recorded output the answer was based on,
        verbatim. Empty when the conclusion is not conclusive.
    ``rationale``
        Why the judge answered the way it did — or, for an inconclusive
        conclusion, why it could not answer.
    ``replacement``
        The proposed replacement criterion, for a broken criterion.
        ``None`` when the answer is not conclusive or when the failure
        was not a broken criterion.
    ``available``
        Whether the triage itself delivered an answer at all.
        ``False`` means the triage **malfunctioned** — the provider
        blew up, the payload was not a JSON object, or the quote the
        answer carried could not be verified against the recorded
        failure. A malfunctioning triage is not evidence about the
        failure, so the caller must not stop the loop on it: it
        diverts into the repair path and leaves the same-cause counter
        alone. ``True`` means the triage ran to completion — the
        conclusion may still be inconclusive (the judge could not
        characterise the failure), but it is a verdict about the
        failure and the caller may act on it.

        The default is ``True``: a conclusion that does not say it
        malfunctioned is taken at its word, because that is the shape
        every conclusion had before the distinction existed.
    """

    vp_id: str
    failure_class: str
    conclusive: bool
    evidence_quote: str = ""
    rationale: str = ""
    replacement: Optional[Dict[str, Any]] = None
    available: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """The conclusion as a plain dict, for the log and the record."""
        return {
            "vp_id": self.vp_id,
            "failure_class": self.failure_class,
            "conclusive": self.conclusive,
            "evidence_quote": self.evidence_quote,
            "rationale": self.rationale,
            "replacement": dict(self.replacement) if self.replacement else None,
            "available": self.available,
        }


# ---------------------------------------------------------------------------
# The recorded text a quote may point at
# ---------------------------------------------------------------------------

#: The record fields whose text the judge may quote.
#:
#: The list is deliberately narrow: it holds the fields that carry the
#: *recorded failure* — the raw output, the evidence, the fragment a
#: previous verdict already anchored, and the reason a repair attempt
#: failed. A quote that is not in one of these is a quote of something
#: the judge was never shown.
_ANCHOR_FIELDS = (
    "actual_result",
    "evidence",
    "evidence_quote",
    "reason",
    "reasons",
    "failure_reason",
    "repair_failure_reason",
)


def _recorded_texts(record: Any) -> List[str]:
    """The quotable texts of one record, one string per field.

    A field that carries a list (the verdict records carry
    ``reasons``) contributes one string per item: a quote of one reason
    is a quote of the record, and joining the list into a single string
    would let a quote span two reasons and match nothing real.

    Args:
        record: A mapping (a verification point, a history entry) or
            anything else — which contributes nothing.

    Returns:
        The non-empty strings the judge may quote.
    """
    if not isinstance(record, Mapping):
        return []
    texts: List[str] = []
    for name in _ANCHOR_FIELDS:
        value = record.get(name)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            texts.extend(
                item for item in value
                if isinstance(item, str) and item.strip()
            )
        elif isinstance(value, str) and value.strip():
            texts.append(value)
    return texts


def _history_entries(failure_history: Any) -> List[Any]:
    """The failure history as a list of entries.

    The history is normally a list of entries — the shape
    ``verification.failure_history`` stores per point. A mapping keyed
    by point id is accepted too: its values are the entries. A caller
    that forgot to select its own point would otherwise hand the judge
    a list of id strings, and the judge would read that as "nothing was
    recorded" rather than as the mistake it is.
    """
    if not failure_history:
        return []
    if isinstance(failure_history, Mapping):
        return list(failure_history.values())
    return list(failure_history)


def _quote_verifies(quote: Any, texts: List[str]) -> bool:
    """Whether ``quote`` appears verbatim in one of the recorded texts.

    The check is per text, not against the concatenation: a quote that
    spans two different records matches only because of the separator
    the concatenation introduced, and that is not a quote of anything.
    A match after whitespace, case or Unicode normalisation is a
    paraphrase and fails — the rule is
    :func:`verification.failure_triage.validate_evidence_quote`, used
    as-is rather than re-implemented.

    Args:
        quote: The fragment the answer was based on.
        texts: The recorded texts the judge was shown.

    Returns:
        ``True`` when the quote is a verbatim substring of one of them.
    """
    if not isinstance(quote, str) or not quote.strip():
        return False
    return any(validate_evidence_quote(quote, text) for text in texts)


# ---------------------------------------------------------------------------
# The judge
# ---------------------------------------------------------------------------


class VPTriageJudge:
    """The semantic tier: one tool-less LLM call that names the failure.

    The judge is constructed with the coding tool it should use, which
    is what makes it testable: the unit tests hand it a recorder and
    assert on the shape of the single call. Without a tool it builds a
    :class:`coding_tool.ClaudeCodingTool` on first use, on the
    ``verification`` scene — the same scene the verification path
    itself runs on.
    """

    #: How long one triage call may take.
    TIMEOUT_SECONDS = 300

    def __init__(
        self,
        coding_tool: Any = None,
        timeout_seconds: Optional[int] = None,
    ) -> None:
        self._coding_tool = coding_tool
        self._timeout = (
            int(timeout_seconds) if timeout_seconds else self.TIMEOUT_SECONDS
        )

    # ------------------------------------------------------------------
    # The tool
    # ------------------------------------------------------------------

    @property
    def coding_tool(self) -> Any:
        """The tool the judge calls — built on first use when absent."""
        if self._coding_tool is None:
            from coding_tool import ClaudeCodingTool

            self._coding_tool = ClaudeCodingTool(scene="verification")
        return self._coding_tool

    # ------------------------------------------------------------------
    # The prompt
    # ------------------------------------------------------------------

    def build_system_prompt(self) -> str:
        """The constant half of the prompt: role, forbid, contract."""
        return (
            "You are a VERIFICATION TRIAGE JUDGE. A verification point "
            "has failed the same way several rounds in a row and the "
            "loop needs to know the NATURE of that failure before it "
            "decides whether to rework the deliverable, retry, escalate "
            "to a human, or skip the check.\n\n"
            "You have NO tools. You cannot run the test, read files, or "
            "look at the code. Your ONLY input is the recorded failure "
            "text in the user prompt, and your ONLY job is to classify "
            "it.\n\n"
            "HARD RULES — violating any of these makes your answer "
            "discarded:\n"
            "1. The `evidence_quote` must be copied CHARACTER FOR "
            "CHARACTER from the recorded failure text. A paraphrase, a "
            "rephrasing, a translation or an invented summary is not a "
            "quote and will be refused.\n"
            "2. If the recorded text does not settle the question, do "
            "not guess: answer with the class that fits the recorded "
            "text as it is.\n"
            "3. Do NOT add prose, commentary or markdown around the "
            "JSON object. The JSON object is the whole answer."
        )

    def build_prompt(
        self,
        vp: Any,
        failure_history: Any,
        round_number: int = 0,
    ) -> str:
        """The per-call half of the prompt: the point, the failures, the
        classes, the output contract.

        The recorded failure text goes in verbatim — it is the only text
        the judge is allowed to quote, so it has to be the only text it
        was given.
        """
        point = vp if isinstance(vp, Mapping) else {}
        vp_id = str(point.get("id") or point.get("vp_id") or "unknown")
        history = _history_entries(failure_history)

        parts: List[str] = [
            f"Verification point: {vp_id}",
            f"Round: {round_number}",
        ]
        for label, key in (
            ("Title", "title"),
            ("Verification method", "verification_method"),
            ("Expected result", "expected_result"),
            ("Test command", "test_command"),
        ):
            value = str(point.get(key) or "").strip()
            if value:
                parts.append(f"{label}: {value}")

        parts.extend([
            "",
            "RECORDED FAILURE TEXT (the only text you may quote from):",
            self._render_history(history),
            "",
            "CLASSES — pick exactly one:",
            "- infra: the environment failed, not the work (rate limit, "
            "timeout, the process was killed, the answer could not be "
            "parsed). Another attempt is the remedy.",
            "- deliverable_defect: the deliverable under verification is "
            "genuinely wrong. The work has to be redone.",
            "- criterion_defect: the criterion itself is wrong — it "
            "cannot be expressed or cannot be run as written. Reworking "
            "the deliverable will never satisfy it; a human has to fix "
            "the criterion.",
            "- out_of_scope: the check asks for something nobody agreed "
            "to build. The remedy is to skip it.",
            "",
            "A `replacement` is required ONLY for criterion_defect: it is "
            "the proposed replacement criterion, with a `title`, a "
            "`verification_method` (one of full_ci, e2e, api_test, "
            "code_review, ui_validation), an `expected_result` and a "
            "`test_command`. For every other class it must be null.",
            "",
            "Return ONLY this JSON object, no markdown, no prose:",
            '{"failure_class": "<infra|deliverable_defect|criterion_defect|'
            'out_of_scope>", "evidence_quote": "<a fragment copied '
            'verbatim from the recorded failure text>", "rationale": '
            '"<one or two sentences>", "replacement": null}',
        ])
        return "\n".join(parts)

    def _render_history(self, history: List[Any]) -> str:
        """The recorded failures, one block per entry.

        An entry with no quotable text is still rendered — with its
        class and its round — so the judge can see that a failure was
        recorded even when the raw text was not kept.
        """
        if not history:
            return "(no recorded failure text)"
        blocks: List[str] = []
        for index, entry in enumerate(history, start=1):
            record = entry if isinstance(entry, Mapping) else {}
            vp_id = str(record.get("id") or record.get("vp_id") or "")
            round_number = record.get("round")
            failure_class = record.get("failure_class")
            header_bits = [f"failure #{index}"]
            if vp_id:
                header_bits.append(f"point {vp_id}")
            if round_number is not None:
                header_bits.append(f"round {round_number}")
            if failure_class:
                header_bits.append(f"recorded class {failure_class}")
            blocks.append("[" + ", ".join(header_bits) + "]")
            texts = _recorded_texts(record)
            if texts:
                blocks.extend(texts)
            else:
                blocks.append("(no text recorded)")
        return "\n".join(blocks)

    # ------------------------------------------------------------------
    # The call
    # ------------------------------------------------------------------

    def judge(
        self,
        vp: Any,
        failure_history: Any,
        round_number: int = 0,
    ) -> TriageConclusion:
        """Classify the failure of one verification point.

        One LLM call, no tools, one answer. Everything that could make
        the answer unusable — an exception from the provider, a payload
        that is not a JSON object, a class outside the vocabulary, a
        quote that is not in the recorded failure — produces a
        conclusion that is not conclusive and carries
        :data:`FALLBACK_CLASS`.

        The conclusions are not all alike, and the difference is the
        one the caller acts on. A triage that **malfunctioned** — the
        provider blew up, the payload was unreadable, the quote did not
        verify — produces a conclusion with ``available=False``: it
        says nothing about the failure, so the caller repairs the
        deliverable instead of stopping the loop on a failure nobody
        classified. A triage that **ran and could not characterise**
        the failure produces ``available=True`` with
        ``conclusive=False``: the failure really is unclassifiable, and
        a human has to look at it.

        Args:
            vp: The verification point. Its ``id`` names the
                conclusion; its ``title`` / ``expected_result`` /
                ``verification_method`` / ``test_command`` go into the
                prompt; its ``actual_result`` / ``evidence`` are
                quotable text.
            failure_history: The recorded failures of this point, most
                recent last. Each entry's recorded text is quotable.
            round_number: The round the loop is in — context for the
                judge, and part of the prompt.

        Returns:
            The :class:`TriageConclusion`. Never raises: a judge that
            raises cannot be called from the loop's own error path.
        """
        point = vp if isinstance(vp, Mapping) else {}
        vp_id = str(point.get("id") or point.get("vp_id") or "")
        history = _history_entries(failure_history)
        quotable = _recorded_texts(point) + [
            text
            for entry in history
            for text in _recorded_texts(entry)
        ]

        try:
            response = self.coding_tool.query_json(
                prompt=self.build_prompt(point, history, round_number),
                system_instruction=self.build_system_prompt(),
                timeout=self._timeout,
                allowed_tools=[],
            )
        except Exception as exc:  # noqa: BLE001 — the judge never raises
            logger.warning(
                "[triage] judge call failed for %s: %s: %s",
                vp_id or "unknown", type(exc).__name__, exc,
            )
            return self._unavailable(
                vp_id,
                f"triage LLM call failed: {type(exc).__name__}: {exc}",
            )

        payload = self._parse_payload(response)
        if payload is None:
            return self._unavailable(
                vp_id, "triage LLM output is not a JSON object",
            )
        return self._conclude(vp_id, payload, quotable)

    # ------------------------------------------------------------------
    # Reading the answer
    # ------------------------------------------------------------------

    def _parse_payload(self, response: Any) -> Optional[Dict[str, Any]]:
        """The answer as a mapping, or ``None`` when it is not one.

        The tool answers with a parsed object, but a caller may hand
        back the raw text instead; both are read, and anything that is
        not an object is not an answer.
        """
        if isinstance(response, Mapping):
            return dict(response)
        if isinstance(response, (str, bytes)):
            try:
                decoded = json.loads(response)
            except (ValueError, TypeError):
                return None
            if isinstance(decoded, Mapping):
                return dict(decoded)
        return None

    def _conclude(
        self,
        vp_id: str,
        payload: Dict[str, Any],
        quotable: List[str],
    ) -> TriageConclusion:
        """Turn a parsed answer into a conclusion, or refuse it."""
        claimed = payload.get("failure_class")
        failure_class = _normalize_class(claimed)
        if failure_class is None:
            return self._inconclusive(
                vp_id,
                f"triage returned a failure_class outside the four-class "
                f"vocabulary: {claimed!r}",
            )

        quote = payload.get("evidence_quote")
        if not isinstance(quote, str) or not quote.strip():
            return self._inconclusive(
                vp_id, "triage returned no evidence quote",
            )
        if not _quote_verifies(quote, quotable):
            # The answer points at text that is not there: the model
            # described what it saw instead of quoting it. That is a
            # failure of the *triage*, not a statement about the
            # failure — the caller must not stop the loop on it.
            return self._unavailable(
                vp_id,
                f"triage evidence quote is not verbatim in the recorded "
                f"failure text: {quote!r}",
            )

        replacement = payload.get("replacement")
        if replacement is not None and not isinstance(replacement, Mapping):
            return self._inconclusive(
                vp_id,
                "triage replacement is not an object: "
                f"{type(replacement).__name__}",
            )

        rationale = payload.get("rationale")
        conclusion = TriageConclusion(
            vp_id=vp_id,
            failure_class=failure_class,
            conclusive=True,
            evidence_quote=quote,
            rationale=rationale if isinstance(rationale, str) else "",
            replacement=dict(replacement) if replacement is not None else None,
        )
        logger.info(
            "[triage] %s → %s (conclusive, quote %d chars)",
            vp_id or "unknown", failure_class, len(quote),
        )
        return conclusion

    def _inconclusive(self, vp_id: str, reason: str) -> TriageConclusion:
        """The conclusion for an answer that could not be checked.

        The triage ran to completion and answered — the answer simply
        does not characterise the failure: the class it named is
        outside the vocabulary, it offered no quote to anchor the
        answer, or the replacement it proposed is not an object. The
        conclusion is *available* (the triage spoke) but not
        conclusive, and the caller decides what to do with a failure
        nobody could name.

        The class is the one that counts, the quote is empty (an
        unverified quote is not an anchor, and carrying it would make
        it look like one), and the replacement is dropped — a
        replacement nobody checked is not a criterion.
        """
        logger.info(
            "[triage] %s → inconclusive (%s)", vp_id or "unknown", reason,
        )
        return TriageConclusion(
            vp_id=vp_id,
            failure_class=FALLBACK_CLASS,
            conclusive=False,
            evidence_quote="",
            rationale=reason,
            replacement=None,
            available=True,
        )

    def _unavailable(self, vp_id: str, reason: str) -> TriageConclusion:
        """The conclusion for a triage that could not answer at all.

        The provider blew up, the payload was not a JSON object, or the
        quote the answer carried appears nowhere in the recorded
        failure. Every one of those is a statement about the *triage*,
        not about the failure it was asked about — so the conclusion is
        marked unavailable, and the caller diverts into the repair path
        instead of stopping the loop over a failure nobody classified.

        The class is still the one that counts and the quote is still
        empty: an unavailable conclusion carries no anchor either.
        """
        logger.warning(
            "[triage] %s → unavailable (%s)", vp_id or "unknown", reason,
        )
        return TriageConclusion(
            vp_id=vp_id,
            failure_class=FALLBACK_CLASS,
            conclusive=False,
            evidence_quote="",
            rationale=reason,
            replacement=None,
            available=False,
        )


__all__ = [
    "FALLBACK_CLASS",
    "TriageConclusion",
    "VPTriageJudge",
]