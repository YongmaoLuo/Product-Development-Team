"""Terminal-round failure analysis — the prompt, and the one call it earns.

Why this module exists
======================

When a verification round stops, the terminal branch asks a human how to
proceed: rerun the round, accept the failure, or let a human write the
repair tasks.  The question it asked carried one sentence of English
facts — ``"verification round 3 stopped with repair_execution_failed: 0
repair task(s) were generated; failed checks: VP-3"``.  Facts are not a
question.  The person answering it has to reconstruct the failure scene
from a sentence, decide what it means, and then answer *in the
vocabulary the system understands*.

This module is the analysis behind that question: it renders the failure
scene into a prompt, makes **one** call with it, and reads the answer
back under a strict contract.  It is deliberately a separate module with
no orchestrator import, so that

* the rendering can be tested on its own — every fact, every cut, every
  bound is asserted without an LLM in the loop;
* the orchestrator, which will import *this* module, is not imported
  *by* it — an import the other way round would be a cycle.

The judge is an **investigator with a leash** (2026-11-06).  It used to
be a reader: it was handed the failure facts and could only repeat them
back, so its analysis came out empty of the two things a human actually
needs — *what was expected versus what happened, per verification
point*, and *why the framework could not iterate on its own*.  Now it
gets the **read-only** investigation tools the main agent has
(``Bash`` / ``Read`` / ``Grep`` / ``Glob`` — the same set as
``binary_rebuild_agent.BinaryRebuildAgent.ALLOWED_TOOLS``) plus the
paths it is allowed to look at (``plan_dir`` / ``project_dir``), so the
chain record, the root-cause reports, the repair tasks and the code are
facts it can go and read instead of facts it has to guess at.  It may
not write anything: the tools it gets cannot change the workspace, and
the prompt says so.

What the prompt guarantees
==========================

**Every failed point is named.**  A judge asked to reason about a round
has to see *which* points failed and what they are called.  A count is
not a scene.

**The facts are carried, and they are bounded.**  The failure class, how
many rounds the point has now failed, how many repair tasks this round
produced and how many points were deferred are the numbers the
recommendation is read against.  Two of them can be long — a verdict is
often a stack trace — so they are cut, and the cut is *marked* with
``…``: a cut that is not marked is a silent omission, and a judge that
cannot tell it is reading a truncated verdict will reason about a
verdict it has not fully seen.

**The points left out are counted out loud.**  More than
:data:`MAX_FAILED_POINTS` failed points are cut, and how many were cut
is written into the prompt.  A judge that sees ten of twelve failures
without being told about the other two does not know it is missing them.

**The answers are spelled out.**  The judge is told which
recommendations exist and what each one means, in the language the human
who reads the result will read.

**The judge is told where to look.**  The prompt carries ``plan_dir`` and
``project_dir``, and the files under them that answer the two questions
the facts cannot: the root-cause chain record, the root-cause reports,
the repair-task snapshot, the verification report.  A judge that is
handed a path is an investigator; a judge that is handed a summary is a
repeater.

**The recommendation vocabulary is the orchestrator's.**  This module
does not import the orchestrator (the orchestrator will import *this*
module), so the two lists of recommendations can drift apart unless
something pins them together.  ``tests/unit/test_terminal_analysis_judge.py``
is that something.

What the call guarantees
========================

**One call, read-only tools.**  The judge is an investigator that may
read but never write: it gets ``Bash`` / ``Read`` / ``Grep`` / ``Glob``
— the same read-only set ``binary_rebuild_agent`` uses — so it can go
and read the chain record, the root-cause reports and the code instead
of answering from the summary it was handed.  It gets no ``Edit`` and
no ``Write``: the analysis is a reading, and a reading that can change
the workspace is not one.

**The answer is accepted whole or not at all.**  Six fields —
``context``, ``problem``, ``evidence``, ``recommendation``,
``recommendation_reason``, ``uncertainty``.  An answer that is missing
one, or whose recommendation is not one of the actions, is
rejected whole rather than half-used: a half-analysis in front of a
human is worse than none, because it reads as an analysis.

**One string of evidence is still evidence.**  The contract asks for an
array of strings, and a judge that found a *single* fact may write it
as a sentence instead of a one-element array.  That answer is not
thrown away over the shape of one field: the string is taken as the
one-element list it means, and the text is kept **verbatim** — it is
the judge's own evidence, and trimming or paraphrasing it would be
rewriting it.  Every other breach of the contract is still refused
whole, and the refusal now says **which** check failed
(:func:`validate_analysis_with_reason`).

**Every failure is a downgrade, never an exception.**  A provider that
raises, an answer that is not JSON, a payload that is not an object, a
recommendation outside the vocabulary — each of them produces ``None``.
``None`` means "no analysis", and the caller's fallback is the factual
question it asked before, so a human is always asked something.

What the rendering guarantees
=============================

**The question has five sections.**  The analysis is a dict; the human
answers a question.  :func:`render_analysis` turns the six contracted
fields into 背景（C）/ 问题（P）/ 证据（E）/ 建议（A）/ 不确定（U）, in
the order a person reads them, with the evidence carried verbatim.

**The question is bounded, and it is bounded by argument order.**  Over
the cap, the evidence is dropped **from its tail** first — the front of
the argument is what the recommendation rests on.  Only then is the
whole 不确定 section dropped, because half an uncertainty is a fact.
Only then is the 问题 section cut — its head kept, the cut marked with
``…`` — with the room the 建议 section needs reserved first: **the
advice survives every cut**, because a question that carries no advice
asks for a decision nobody can make.

**An analysis that is not one renders as nothing.**  Not a mapping, a
missing field, a recommendation outside the actions — all of them
produce ``""``, and the caller's fallback is the factual question it
asked before.

What the card guarantees
========================

**A report and a card are two renderings of one analysis.**
:func:`render_analysis` is the report: it carries the argument, and it
is written to the file.  :func:`render_brief` is the card: it is what
the human reads in the chat window to answer one question — *what do I
do about this failed round*.  A real analysis is about 3.2KB of CPEA;
the person reading it had to dig out of it what they were being asked
to decide.  The card does not shorten the report, it replaces it where
a question is asked.

**The card is bounded, and it is bounded by what it is for.**  Six
hundred characters and eight lines: the scene, the options, the advice
and the path of the report.  The 问题 / 证据 / 不确定 sections go to the
report, and the card keeps the context's *first sentence*.

**The cut order keeps the argument whole.**  When the card is still too
long, the *reason* is reduced to its **first sentence** first — it is
the one part of the card that is an argument rather than a fact, and
its opening is what the decision rests on.  Only then is the background
sentence cut, and only after that is the reason shortened past its
first sentence.  **The option lines, the recommendation id and the
report path are never cut**: they are what the human acts on.

What the fallback card guarantees
=================================

**A round that stopped without an analysis is still a decision.**
:func:`render_fallback_brief` is the card the human is asked when the
judge produced nothing usable — no answer at all, or an answer that
failed the contract.  It has the **same decision shape** as
:func:`render_brief`, with the failed points written out by hand
instead of by a model, and it says **why** there is no analysis
(``analysis_empty``, or ``analysis_rejected:<reason>``).

**The failed points are named, and the ones that do not fit are
counted.**  Three points are written out with their id, their status
and their title; the rest are counted on the card, so a reader who is
told "there were seven more" can go and read the report.

**The cut order is the argument order.**  The *actual result* is cut
first — it is evidence, and a shorter evidence still names the failure
— and only then is a failed point dropped.  **The point ids, the
statuses, the option lines, the advice and the report path are never
cut**: they are what the human acts on.

**The fallback card has its own bounds.**  Twelve lines and nine
hundred characters: it is longer than the analysis card because it
has to name the failed points, but it is still a card and not a
report.

What this module deliberately does NOT do
==========================================

It does not decide what to do with a failure — it recommends, and the
human decides.  It does not import the orchestrator, and it does not
touch the decision schema: the analysis is read into the body of a
question, and nothing else about the decision changes.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only
    from coding_tool import JsonSession

__all__ = [
    "ANALYSIS_ERROR_EMPTY",
    "ANALYSIS_ERROR_REJECTED",
    "MAX_EXPECTED_RESULT_CHARS",
    "MAX_FAILED_POINTS",
    "MAX_REASON_CHARS",
    "READ_ONLY_TOOLS",
    "RECOMMENDATION_CONSEQUENCES",
    "RECOMMENDATIONS",
    "RECOMMENDATION_LABELS",
    "TERMINAL_BRIEF_MAX_CHARS",
    "TERMINAL_BRIEF_MAX_LINES",
    "TERMINAL_FALLBACK_BRIEF_MAX_CHARS",
    "TERMINAL_FALLBACK_BRIEF_MAX_LINES",
    "TERMINAL_FALLBACK_MAX_POINTS",
    "TERMINAL_QUESTION_MAX_CHARS",
    "TerminalDecisionJudge",
    "render_analysis",
    "render_brief",
    "render_fallback_brief",
    "validate_analysis_with_reason",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

#: The answers a human can give at the terminal round.
#:
#: These are the orchestrator's terminal options, written out here rather
#: than imported from :mod:`verification.orchestrator`: the orchestrator
#: will import *this* module, so importing it the other way round would
#: be a cycle.  ``tests/unit/test_terminal_analysis_judge.py`` pins the
#: two lists together, so a drift is a test failure and not a silent
#: divergence.
RECOMMENDATIONS: Tuple[str, ...] = (
    "rerun_this_round",
    "revise_criterion",
)

#: What each recommendation means, in the language the human who reads
#: the result will read.  The ids stay English because they are the
#: vocabulary the orchestrator matches on; the meaning is Chinese because
#: the person answering is Chinese-speaking.
RECOMMENDATION_LABELS: Dict[str, str] = {
    "rerun_this_round": "重跑本轮验证",
    "revise_criterion": "修订判据（本轮失败的判据作废，由操作者另行补充新判据）",
}

#: What each recommendation costs, in one sentence.
#:
#: The label says what the option *is*; this says what it *does*.  A
#: person choosing between two options needs the consequence more than
#: they need the name of the action, and the name alone does not tell
#: them what they are agreeing to.  Every id in :data:`RECOMMENDATIONS`
#: has an entry here: an option with no consequence stated is not an
#: option a person can decide on.
RECOMMENDATION_CONSEQUENCES: Dict[str, str] = {
    "rerun_this_round": "环境偶发失败，重跑一轮可能就通过",
    "revise_criterion": "判据本身有问题，需要人重写判据",
}

#: The tools the judge may use to investigate.
#:
#: **Read-only, on purpose.**  The judge is asked to *understand* a
#: failure, not to repair one: the repair belongs to the next round, and
#: a judge that could edit the workspace would be editing it from
#: inside a call whose answer is allowed to be thrown away.  The set is
#: the same one ``binary_rebuild_agent.BinaryRebuildAgent`` uses, so
#: both agents investigate the repository the same way.
READ_ONLY_TOOLS: Tuple[str, ...] = ("Bash", "Read", "Grep", "Glob")

#: How many failed points are rendered before the rest are cut.
MAX_FAILED_POINTS = 10

#: How much of a verdict (the failure's reason) is rendered.
MAX_REASON_CHARS = 800

#: How much of an expected result is rendered.
MAX_EXPECTED_RESULT_CHARS = 300

#: How long the terminal question may be, in characters.
#:
#: The question is read by a human in a chat window, and it has to carry
#: five sections of argument; a question that runs to ten thousand
#: characters is not read, it is scrolled past.  Two thousand characters
#: is roughly a screen of Chinese prose — enough for the scene, the
#: problem, the evidence and the uncertainty, and no more.
TERMINAL_QUESTION_MAX_CHARS = 2000

#: How long the decision card may be, in characters.
#:
#: The card is what a human reads in a chat window to answer one
#: question: *what do I do about this failed round*.  Six hundred
#: characters is about a screen of Chinese prose — enough for the
#: scene, the options and the advice, and no more.
TERMINAL_BRIEF_MAX_CHARS = 600

#: How many lines the decision card may have.
#:
#: The card is eight lines by construction: the heading, a blank line,
#: ``选项：``, one line per option, a blank line, the advice, and the
#: path of the full report.  A line is a line the reader scans; a card
#: that scrolls is a report again, which is what the report is for.
#: Every text that goes on the card is folded into one line first, so
#: the judge's line breaks cannot make the card longer than this.
TERMINAL_BRIEF_MAX_LINES = 8


# ---------------------------------------------------------------------------
# The fallback card's own caps
# ---------------------------------------------------------------------------

#: How long the fallback card may be, in characters.
#:
#: The fallback card is longer than the analysis card because it has to
#: name the failed points: the analysis card may point at a report and
#: let the reader go there, the fallback card has no analysis to point
#: at, so the points themselves are what the reader has.  Nine hundred
#: characters is about a screen and a half of Chinese prose — the
#: failed points, the options, the advice and the path of the report.
TERMINAL_FALLBACK_BRIEF_MAX_CHARS = 900

#: How many lines the fallback card may have.
#:
#: Twelve lines by construction: the heading, a blank line,
#: ``失败点：``, three failed points, the count of the ones that did not
#: fit, ``选项：``, the two options, the advice and the path of the
#: report.  A line is a line the reader scans; a card that scrolls is a
#: report again, which is what the report is for.
TERMINAL_FALLBACK_BRIEF_MAX_LINES = 12

#: How many failed points the fallback card names.
#:
#: The rest are counted, not dropped: a reader who is told "there were
#: seven more" can go and read the report, a reader who is told nothing
#: cannot.
TERMINAL_FALLBACK_MAX_POINTS = 3


# ---------------------------------------------------------------------------
# The answer's contract
# ---------------------------------------------------------------------------

#: The text fields an answer must carry.  The model is asked for exactly
#: these names, and an answer is accepted only when it carries exactly
#: these — a field that is missing, or that says nothing, is an answer
#: that was not produced under the contract.
_ANALYSIS_TEXT_FIELDS: Tuple[str, ...] = (
    "context",
    "problem",
    "recommendation_reason",
    "uncertainty",
)

#: The reason an answer is refused for its evidence.
#:
#: It is a phrase, not a sentence: the caller carries it into a card as
#: ``analysis_rejected:<reason>``, and it has to read as the name of the
#: check that failed.  Every way of getting the evidence wrong — an
#: empty string, a blank string, an empty array, an array holding an
#: empty or a non-string item — is the same breach: *the evidence is
#: not a list of strings*.
_EVIDENCE_REASON = "evidence must be a list of strings"

#: The reason an answer is missing **altogether**.
#:
#: It is a phrase, not a sentence: the caller carries it into a card as
#: ``analysis_empty``, and it has to read as the name of the situation
#: rather than as a report of it.  There is no answer to hold against
#: a check — the call raised, the answer was not a JSON object, or
#: there was no answer at all — so the reason names exactly that, and
#: names no check it cannot name.
ANALYSIS_ERROR_EMPTY = "analysis_empty"

#: The prefix a refusal carries: ``analysis_rejected:<reason>``.
#:
#: The reason behind the prefix is the one
#: :func:`validate_analysis_with_reason` gives: the name of the check
#: that failed.  The prefix says *that* the answer was refused, and
#: the reason says *which* check refused it.
ANALYSIS_ERROR_REJECTED = "analysis_rejected:"


# ---------------------------------------------------------------------------
# Reading the answer
# ---------------------------------------------------------------------------


def _parse_payload(response: Any) -> Optional[Dict[str, Any]]:
    """The answer as a mapping, or ``None`` when it is not one.

    The tool answers with a parsed object, but a caller may hand back
    the raw text instead; both are read.  Anything that is not an
    object — a list, a number, a sentence that is not JSON — is not an
    answer, and the call falls back to the factual question.
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


def _validate_analysis(payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The answer as the six contracted fields, or ``None``.

    The contract is strict on purpose.  An analysis that is missing a
    section, or whose recommendation is not one of the actions the
    question offers, is not an analysis the caller can put in front of a
    human — so it is rejected whole rather than half-used.

    Callers that need to know *which* check failed use
    :func:`validate_analysis_with_reason` instead; this entry point
    keeps its old signature, because a caller that only wants the
    analysis has no use for the reason.
    """
    analysis, _reason = validate_analysis_with_reason(payload)
    return analysis


def validate_analysis_with_reason(
    payload: Mapping[str, Any],
) -> Tuple[Optional[Dict[str, Any]], str]:
    """The answer as the six contracted fields, and why it was refused.

    The same contract as :func:`_validate_analysis`, plus the reason
    the answer was refused.  A bare ``None`` says *that* the answer was
    no good; it does not say *which* part of it was not, and a caller
    that renders a fallback for a human has to name the part.  So the
    reason is a short phrase naming the check that failed — the caller
    can carry it into a card (``analysis_rejected:evidence must be a
    list of strings``) without parsing anything.

    Args:
        payload: The answer as a mapping, as
            :func:`_parse_payload` read it.

    Returns:
        ``(analysis, "")`` when the answer holds the contract, and
        ``(None, reason)`` when it does not.  The reason is never empty
        on a refusal, and it is always about the *answer* — not about
        the model, the provider or the round.
    """
    recommendation = payload.get("recommendation")
    if not isinstance(recommendation, str):
        return None, "recommendation must be a string"
    recommendation = recommendation.strip()
    if recommendation not in RECOMMENDATIONS:
        return None, (
            "recommendation must be one of: " + ", ".join(RECOMMENDATIONS)
        )

    text_fields: Dict[str, str] = {}
    for name in _ANALYSIS_TEXT_FIELDS:
        if name not in payload:
            return None, f"missing required field: {name}"
        value = payload.get(name)
        if not isinstance(value, str):
            return None, f"field {name} must be a string"
        text = value.strip()
        if not text:
            # Present but empty: a section that says nothing is a
            # section that is not there.
            return None, f"field {name} must not be empty"
        text_fields[name] = text

    if "evidence" not in payload:
        return None, "missing required field: evidence"
    evidence, reason = _normalize_evidence(payload.get("evidence"))
    if evidence is None:
        return None, reason

    return {
        "context": text_fields["context"],
        "problem": text_fields["problem"],
        "evidence": evidence,
        "recommendation": recommendation,
        "recommendation_reason": text_fields["recommendation_reason"],
        "uncertainty": text_fields["uncertainty"],
    }, ""


def _normalize_evidence(raw: Any) -> Tuple[Optional[List[str]], str]:
    """The evidence as a list of strings, or ``None`` and the reason.

    The contract asks for an **array of strings**.  A judge that found
    exactly one fact may write it as a sentence instead of a
    one-element array — and that answer is right in every other field.
    Refusing it over the shape of one field is how a terminal round
    ended up with no analysis at all, so a string is taken as the
    one-element array it means.

    The text is kept **verbatim**: it is the judge's own evidence, and
    trimming it would be rewriting it.  Only the emptiness test looks
    at the trimmed form — a string that says nothing is not evidence,
    however it is padded.

    An array (or tuple) is normalised exactly as it always was: each
    item is trimmed, and an array holding an empty or a non-string
    item is refused.
    """
    if isinstance(raw, str):
        if not raw.strip():
            return None, _EVIDENCE_REASON
        return [raw], ""

    if not isinstance(raw, (list, tuple)) or not raw:
        return None, _EVIDENCE_REASON
    evidence: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None, _EVIDENCE_REASON
        text = item.strip()
        if not text:
            return None, _EVIDENCE_REASON
        evidence.append(text)
    return evidence, ""


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """Render one field value as text.

    A field that is missing or ``None`` renders as an empty string — the
    line it belongs to is then left out of the prompt entirely.  The
    word ``None`` must never reach the model: it is not a fact about the
    failure, it is an artefact of the recorder, and a judge that reads it
    may read it as a value.
    """
    if value is None:
        return ""
    return str(value).strip()


def _truncate(text: str, limit: int) -> str:
    """Cut ``text`` at ``limit`` characters, marking the cut with ``…``.

    The mark is not decoration.  An unmarked cut reads as a complete
    value, and a judge reasoning about a verdict it has only seen half
    of will not know that it is reasoning about half.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _render_point(point: Any) -> List[str]:
    """Render one failed verification point as prompt lines.

    A point that is not a mapping is rendered as an empty point rather
    than raising: the prompt is a rendering of a recorded scene, and a
    scene with one unreadable entry in it is still a scene.
    """
    record: Mapping[str, Any] = point if isinstance(point, Mapping) else {}

    point_id = _text(record.get("id"))
    title = _text(record.get("title"))

    lines: List[str] = []
    heading = " — ".join(part for part in (point_id, title) if part)
    if heading:
        lines.append(f"### {heading}")

    fields: Tuple[Tuple[str, str, Optional[int]], ...] = (
        ("id", "id", None),
        ("title", "title", None),
        ("expected_result", "expected_result", MAX_EXPECTED_RESULT_CHARS),
        ("failure_class", "failure_class", None),
        ("consecutive_rounds", "consecutive_rounds", None),
        ("verdict", "verdict", MAX_REASON_CHARS),
    )
    for label, key, limit in fields:
        rendered = _text(record.get(key))
        if not rendered:
            # Missing or None: the line renders as an empty string.
            continue
        if limit is not None:
            rendered = _truncate(rendered, limit)
        lines.append(f"- {label}: {rendered}")

    return lines


# ---------------------------------------------------------------------------
# The question's body
# ---------------------------------------------------------------------------


def _clean_body(text: str) -> str:
    """A section body as one block: no blank lines, no edge whitespace.

    The blank lines are removed rather than collapsed into one: a
    section is a paragraph, and a paragraph with a hole in the middle of
    it reads as two paragraphs.  The lines that survive keep their
    spacing — the text is the judge's, not this module's, and rewriting
    it would be paraphrasing evidence.
    """
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _section(heading: str, body: str) -> str:
    """One ``## heading`` + body block."""
    return f"## {heading}\n{body}"


def _cut_head_to_budget(text: str, budget: int) -> str:
    """Cut ``text`` down to ``budget`` characters, keeping its head.

    The cut is marked with ``…``: an unmarked cut reads as a complete
    value, and a human reasoning about a verdict it has only seen half
    of will not know that it is reasoning about half.
    """
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    if budget == 1:
        return "…"
    return text[: budget - 1] + "…"


def _fit_problem_to_budget(problem: str, budget: int) -> str:
    """The problem section, fitted into ``budget`` characters.

    The head is kept and the tail is cut.  The problem section opens
    with the scene the recommendation rests on — what was expected and
    what actually happened — and its tail is the per-point detail that
    the recommendation itself summarises.  Cutting the head instead
    would leave a problem whose beginning is gone, and a recommendation
    with nothing under it.
    """
    return _cut_head_to_budget(problem, budget)


def _render_cpea(
    *,
    context: str,
    problem: str,
    evidence: List[str],
    recommendation: str,
    recommendation_reason: str,
    uncertainty: Optional[str],
) -> str:
    """The five C/P/E/A/U sections, joined by exactly one blank line.

    The evidence section is left out when there is no evidence left to
    show — an empty section under a heading reads as "the judge had
    nothing", which is a different claim from "the judge said this".
    The uncertainty section is dropped whole rather than cut, because
    half an uncertainty is a fact.
    """
    sections: List[str] = [
        _section("背景（C）", _clean_body(context)),
        _section("问题（P）", _clean_body(problem)),
    ]

    if evidence:
        bullets = "\n".join(f"- {item}" for item in evidence)
        sections.append(_section("证据（E）", bullets))

    sections.append(
        _section(
            "建议（A）",
            f"{recommendation} —— {recommendation_reason}",
        )
    )

    if uncertainty is not None:
        sections.append(_section("不确定（U）", _clean_body(uncertainty)))

    return "\n\n".join(sections)


def render_analysis(
    analysis: Mapping[str, Any],
    limit: int = TERMINAL_QUESTION_MAX_CHARS,
) -> str:
    """Render a six-field analysis into the terminal question's body.

    The judge answers with a dict; the human answers with a click on a
    button, and the thing between them is a question.  This is the
    question: five sections — 背景 / 问题 / 证据 / 建议 / 不确定 — in the
    order a person reads them.

    The question is bounded, and it is bounded **by argument order**.
    When the rendered text is too long, the tail of the evidence goes
    first: the front of the argument is what the recommendation rests
    on, and a judge that wrote six pieces of evidence wrote them in
    order of importance.  Only when dropping evidence was not enough is
    the whole 不确定 section dropped — an uncertainty is either said or
    not said, and half of one is a fact.

    What is left after that is the 建议 section, and it is **kept, in
    full, at any cost**: the advice is what the human is asked to act
    on, and a question that has no advice in it asks for a decision
    nobody can make.  So the 问题 section is cut next — its head kept,
    its tail dropped, the cut marked with ``…`` — with the room the
    advice needs reserved first.  Only when that was not enough either
    is the text cut, with the cut marked by ``…``; even then the advice
    survives, because it is put back after the cut.

    The returned string is never longer than ``limit``.

    Args:
        analysis: The six contracted fields — ``context``, ``problem``,
            ``evidence``, ``recommendation``, ``recommendation_reason``
            and ``uncertainty``.
        limit: The cap, in characters.

    Returns:
        The rendered question, or ``""`` when the analysis is not one —
        not a mapping, missing a field, or a recommendation outside the
        actions the question offers.  ``""`` means "no analysis",
        and the caller's fallback is the factual question it asked
        before, so a human is always asked something.
    """
    if not isinstance(analysis, Mapping):
        return ""

    validated = _validate_analysis(analysis)
    if validated is None:
        return ""

    try:
        max_chars = int(limit)
    except (TypeError, ValueError):
        return ""
    if max_chars <= 0:
        return ""

    context = validated["context"]
    problem = validated["problem"]
    evidence = list(validated["evidence"])
    recommendation = validated["recommendation"]
    recommendation_reason = validated["recommendation_reason"]
    uncertainty = validated["uncertainty"]

    # The bodies are cleaned once, here: the cut in step 3 measures the
    # text that is actually rendered, and cleaning is idempotent, so
    # cleaning them again inside ``_render_cpea`` changes nothing.
    context_body = _clean_body(context)
    problem_body = _clean_body(problem)

    def build(items: List[str], keep_uncertainty: bool) -> str:
        return _render_cpea(
            context=context_body,
            problem=problem_body,
            evidence=items,
            recommendation=recommendation,
            recommendation_reason=recommendation_reason,
            uncertainty=uncertainty if keep_uncertainty else None,
        )

    rendered = build(evidence, True)
    if len(rendered) <= max_chars:
        return rendered

    # 1. The evidence tail goes first — the front of the argument stays.
    trimmed = list(evidence)
    while trimmed and len(rendered) > max_chars:
        trimmed.pop()
        rendered = build(trimmed, True)
    if len(rendered) <= max_chars:
        return rendered

    # 2. The uncertainty section goes whole.
    rendered = build(trimmed, False)
    if len(rendered) <= max_chars:
        return rendered

    # 3. The 问题 section is cut, its head kept, and the room the advice
    #    needs is reserved first: the advice is what the human is asked
    #    to act on, and it must survive every cut below.
    advice_body = f"{recommendation} —— {recommendation_reason}"
    advice_section = _section("建议（A）", advice_body)
    context_section = _section("背景（C）", context_body)

    if len(advice_section) + 2 > max_chars:
        # Even the advice alone does not fit: its reason is cut, its
        # heading and its recommendation id are not.
        room = max_chars - len("## 建议（A）") - 1 - 2
        advice_body = _cut_head_to_budget(advice_body, room)
        advice_section = _section("建议（A）", advice_body)

    # The sections that will be rendered are 背景 / 问题 / (证据) / 建议,
    # each pair separated by exactly one blank line — the same join
    # ``_render_cpea`` uses, so the two renderings are the same text.
    # Everything but the 问题 body has a fixed size, so the room the
    # 问题 body gets is what is left of the cap.
    #
    # The evidence is gone by now: step 1 leaves evidence behind only
    # when the text fits, and that case has already returned.  The
    # branch below is the safety net for that invariant, not the path
    # this rendering takes.
    problem_heading = _section("问题（P）", "")
    evidence_section = (
        _section("证据（E）", "\n".join(f"- {item}" for item in trimmed))
        if trimmed
        else ""
    )
    overhead = (
        len(context_section)
        + len(problem_heading)
        + len(advice_section)
        + 2 * 2
        + (2 + len(evidence_section) if evidence_section else 0)
    )
    budget = max_chars - overhead

    front_sections = [
        context_section,
        problem_heading + _fit_problem_to_budget(problem_body, budget),
    ]
    if evidence_section:
        front_sections.append(evidence_section)
    front = "\n\n".join(front_sections)

    result = f"{front}\n\n{advice_section}"
    if len(result) <= max_chars:
        return result

    # 4. The hard cut, marked so that it does not read as a complete
    #    text — and the advice survives it: the cut takes from the
    #    front, and the advice is put back after it.
    allowance = max_chars - len(advice_section) - 2
    if allowance < 1:
        return advice_section
    if len(front) > allowance:
        front = front[: allowance - 1] + "…"

    result = f"{front}\n\n{advice_section}"
    if len(result) > max_chars:
        # Only reachable for a limit too small to hold the advice's own
        # heading: the length promise wins over the advice.
        result = result[: max_chars - 1] + "…"
    return result


# ---------------------------------------------------------------------------
# The decision card
# ---------------------------------------------------------------------------

#: The characters a sentence may end with.  The card's first line
#: carries the *first sentence* of the context and nothing else: the
#: rest of the context is the report's business, and the report is
#: where the reader goes when the card is not enough.
_SENTENCE_BREAKS = re.compile(r"[。！？\n]")

#: The characters a single sentence ends with.  A card renders
#: sentences, and a sentence on a card does not carry its terminator:
#: the card is not quoting the analysis, it is naming the scene.
_SENTENCE_END = "。！？"


def _first_sentence(text: str) -> str:
    """The first sentence of ``text``.

    The cut is at the first sentence break and the break itself is
    dropped: the card is not quoting the context, it is naming the
    scene.
    """
    cleaned = _clean_body(text)
    match = _SENTENCE_BREAKS.search(cleaned)
    if match is not None:
        cleaned = cleaned[: match.start()]
    return cleaned.strip()


#: The reason's sentence breaks: the CJK breaks the card already uses,
#: plus an ASCII terminator only when a new sentence visibly starts after
#: it (whitespace + uppercase).  The lookahead is what keeps ``v1.2``,
#: ``0.5`` and ``e.g. something`` in one piece.
_REASON_SENTENCE_BREAKS = re.compile(r"[。！？]|(?<=[.!?])\s+(?=[A-Z])")


def _first_sentence_of_reason(text: str) -> str:
    """The reason's first sentence, CJK or ASCII, without its terminator."""
    match = _REASON_SENTENCE_BREAKS.search(text)
    head = (text[: match.start()] if match is not None else text).strip()
    if head.endswith((".", "!", "?")):
        head = head[:-1].rstrip()
    return head


def _as_sentence(text: str) -> str:
    """The text as one sentence, without its terminator.

    The reason is a sentence the judge wrote; a card that quotes it
    with the full stop still reads as a quotation of the report, and
    the report is one click away.
    """
    stripped = text.strip()
    if stripped.endswith(tuple(_SENTENCE_END)):
        stripped = stripped[:-1].rstrip()
    return stripped


def _option_name(label: str) -> str:
    """The option's name, without the gloss the label spells out.

    :data:`RECOMMENDATION_LABELS` says what an option *is*, and for
    one of them it also says what it means in full.  The option list
    may afford the full spelling — that is where the option is
    *defined* — but the advice line only has to name the option it
    recommends, and a name spelled out twice in one card is noise.
    """
    name = label.split("（", 1)[0].strip()
    return name or label


def _point_ids(values: Any) -> List[str]:
    """The failed point ids, cleaned.

    A single id handed over as a string is one id and not a string of
    characters: the caller may pass either.
    """
    if values is None:
        return []
    if isinstance(values, str):
        candidates: Iterable[Any] = (values,)
    else:
        try:
            candidates = list(values)
        except TypeError:
            return []
    ids: List[str] = []
    for value in candidates:
        text = _text(value)
        if text:
            ids.append(text)
    return ids


def _round_number(value: Any) -> int:
    """The round number, or zero when it is not one."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


#: The characters ``str.splitlines()`` cuts a line at.  The card's line
#: count is measured with ``splitlines()``, so every one of them has to
#: be folded away before a text is put on the card: a vertical tab is as
#: much a line break as a newline is.
_LINE_BREAKS = re.compile(r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]+")


def _one_line(text: str) -> str:
    """The text as a single line.

    Every run of line breaks becomes one space, and the edges are
    stripped: the card is eight lines *by construction*, and a text the
    judge wrote over several lines must not add lines to it.

    The spaces *inside* the text are left as they are — the text is the
    judge's, and rewriting it would be paraphrasing it.
    """
    return _LINE_BREAKS.sub(" ", text).strip()


def _render_brief_card(
    *,
    plan_id: str,
    round_number: int,
    point_ids: List[str],
    background: str,
    reason: str,
    recommendation: str,
    report_path: str,
) -> str:
    """The card's lines, joined by newlines.

    The shape is fixed: what has to be decided, the options, the advice,
    and where the whole analysis lives.  Nothing is cut here — the cuts
    happen before the card is built, so that the parts which are never
    cut are never cut *in half* either.

    Every text that comes from outside — the plan id, the point ids, the
    background, the reason, the path of the report — is folded into one
    line first.  The card's line count is therefore its *shape* and not
    the judge's line breaks: a reason written over three lines cannot
    push the card past :data:`TERMINAL_BRIEF_MAX_LINES`.
    """
    plan = _one_line(plan_id)
    points = [point for point in (_one_line(pid) for pid in point_ids) if point]
    scene = _one_line(background)
    why = _one_line(reason)
    path = _one_line(report_path)

    title_parts: List[str] = []
    if plan:
        title_parts.append(plan)
    if round_number:
        title_parts.append(f"第 {round_number} 轮验证终止")
    title = " ".join(title_parts)

    heading = "【需要你决定】"
    if title:
        heading += title
    if points:
        heading += "：" + "、".join(points)
    if scene:
        heading += "：" + scene

    lines: List[str] = [heading, "", "选项："]
    for index, option in enumerate(RECOMMENDATIONS, start=1):
        label = RECOMMENDATION_LABELS.get(option, option)
        consequence = RECOMMENDATION_CONSEQUENCES.get(option, "")
        line = f"{index}. {label}（{option}）"
        if consequence:
            line += f"—— {consequence}"
        lines.append(line)

    lines.append("")

    advice = (
        f"建议：{_option_name(RECOMMENDATION_LABELS.get(recommendation, recommendation))}"
        f"（{recommendation}）"
    )
    if why:
        advice += f"—— {why}"
    lines.append(advice)

    if path:
        lines.append(f"完整分析：{path}")

    return "\n".join(lines)


def _shrink_to_fit(
    render: Callable[[str], str],
    text: str,
    limit: int,
) -> str:
    """The longest prefix of ``text`` that ``render`` fits within ``limit``.

    The cut is marked with ``…``: an unmarked cut reads as a complete
    sentence, and a person acting on half a sentence does not know they
    are acting on half.
    """
    if len(render(text)) <= limit:
        return text
    if not text:
        return ""

    low, high = 0, len(text)
    best = ""
    while low <= high:
        middle = (low + high) // 2
        candidate = _cut_head_to_budget(text, middle)
        if len(render(candidate)) <= limit:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def render_brief(
    analysis: Mapping[str, Any],
    *,
    plan_id: str = "",
    round_number: int = 0,
    failed_point_ids: Sequence[str] = (),
    report_path: str = "",
    limit: int = TERMINAL_BRIEF_MAX_CHARS,
) -> str:
    """Render a six-field analysis into the card a human answers.

    The report (:func:`render_analysis`) is long because a report has to
    carry the argument.  A *question* has to be answered: the person
    reading this card has to see what stopped, what they may choose,
    which choice is recommended and where the full analysis lives —
    without scrolling, and without reading it twice.

    So the card drops what the card does not need: the problem section,
    the evidence and the uncertainty go to the report, and the card
    keeps the context's **first sentence**, the options, the advice and
    the path of the report.

    **The cut order keeps the argument whole.**  When the card is still
    too long, the *reason* is reduced to its **first sentence** first —
    the reason is the one part of the card that is an argument rather
    than a fact, and the argument's opening is what the decision rests
    on.  Only then is the background sentence cut, and only after that
    is the reason shortened past its first sentence.  The option lines,
    the recommendation id and the report path are **never** cut: they
    are what the human acts on, and a card that omits them asks for a
    decision nobody can make.

    The card has at most :data:`TERMINAL_BRIEF_MAX_LINES` lines: its
    shape is fixed, and every text that goes on it is folded into one
    line first, so the judge's line breaks cannot push the card past
    the cap.  Its *length* is kept within ``limit`` by reducing the
    reason to its first sentence, then cutting the background, and only
    then cutting the reason further; when the parts of the card that are
    never cut are longer than ``limit`` on their own, the card is longer
    than ``limit`` — those parts are what the human acts on, and a card
    that omits them asks for a decision nobody can make.

    Args:
        analysis: The six contracted fields — ``context``, ``problem``,
            ``evidence``, ``recommendation``, ``recommendation_reason``
            and ``uncertainty``.
        plan_id: The plan the round belongs to.
        round_number: The round that stopped.
        failed_point_ids: The ids of the points that failed.
        report_path: Where the full analysis was written.
        limit: The cap, in characters.

    Returns:
        The card, or ``""`` when the analysis is not one — not a
        mapping, missing a field, or a recommendation outside the
        actions the card offers.  ``""`` means "no card", and the
        caller's fallback is the factual sentence it asked before, so a
        human is always asked something.
    """
    if not isinstance(analysis, Mapping):
        return ""

    validated = _validate_analysis(analysis)
    if validated is None:
        return ""

    try:
        max_chars = int(limit)
    except (TypeError, ValueError):
        return ""
    if max_chars <= 0:
        return ""

    recommendation = validated["recommendation"]
    # The reason is folded into one line *before* it is measured and
    # cut: the cut has to count the characters the card actually shows,
    # and the card shows the reason on one line.
    reason = _one_line(_as_sentence(validated["recommendation_reason"]))
    background = _first_sentence(validated["context"])
    points = _point_ids(failed_point_ids)

    def build(scene: str, why: str) -> str:
        return _render_brief_card(
            plan_id=str(plan_id or ""),
            round_number=_round_number(round_number),
            point_ids=points,
            background=scene,
            reason=why,
            recommendation=recommendation,
            report_path=str(report_path or ""),
        )

    card = build(background, reason)
    if len(card) <= max_chars:
        return card

    # 1. The reason is an argument: keep its first sentence, the facts keep their place.
    lead = _first_sentence_of_reason(reason)
    if lead and lead != reason:
        reason = lead
        card = build(background, reason)
        if len(card) <= max_chars:
            return card

    # 2. The background sentence goes next.
    background = _shrink_to_fit(
        lambda scene: build(scene, reason), background, max_chars
    )
    card = build(background, reason)
    if len(card) <= max_chars:
        return card

    # 3. Only then is the reason shortened past its first sentence.
    reason = _shrink_to_fit(
        lambda why: build(background, why), reason, max_chars
    )
    return build(background, reason)


# ---------------------------------------------------------------------------
# The fallback card
# ---------------------------------------------------------------------------


def _fallback_point(record: Any) -> Dict[str, str]:
    """One failed point, as the four fields the card renders.

    A point that is not a mapping renders as an empty point rather than
    raising: the card is a rendering of a recorded scene, and a scene
    with one unreadable entry in it is still a scene.
    """
    mapping: Mapping[str, Any] = record if isinstance(record, Mapping) else {}
    return {
        "id": _one_line(_text(mapping.get("id"))),
        "status": _one_line(_text(mapping.get("status"))),
        "title": _one_line(_text(mapping.get("title"))),
        "actual_result": _one_line(_text(mapping.get("actual_result"))),
    }


def _fallback_point_line(
    point: Mapping[str, str], detail_limit: Optional[int],
) -> str:
    """One failed point, as the card's line for it.

    The line reads ``<id> <status> —— <title>：<the actual result>``.
    A missing title or a missing actual result leaves its own part
    empty — the rest of the line is still worth reading.
    """
    line = " ".join(
        part for part in (point.get("id", ""), point.get("status", "")) if part
    )
    title = point.get("title", "")
    if title:
        line += f" —— {title}"
    detail = _first_sentence(point.get("actual_result", ""))
    if detail_limit is not None:
        detail = _cut_head_to_budget(detail, detail_limit)
    if detail:
        line += f"：{detail}"
    return line


def _render_fallback_card(
    *,
    plan_id: str,
    round_number: int,
    stop_reason: str,
    points: List[Dict[str, str]],
    remaining: int,
    report_path: str,
    analysis_error: str,
    detail_limit: Optional[int],
) -> str:
    """The fallback card's lines, joined by newlines.

    The shape is the decision card's shape — what has to be decided,
    the options, the advice, where the report lives — with the failed
    points written out by hand where the analysis card would have
    quoted the judge.
    """
    title_parts: List[str] = []
    if plan_id:
        title_parts.append(plan_id)
    if round_number:
        title_parts.append(f"第 {round_number} 轮验证终止")
    title = " ".join(title_parts)

    heading = "【需要你决定】"
    if title:
        heading += title
    if stop_reason:
        heading += f"：{stop_reason}"

    lines: List[str] = [heading, "", "失败点："]
    if not points:
        lines.append("（无失败点记录）")
    else:
        for index, point in enumerate(points, start=1):
            lines.append(f"{index}. {_fallback_point_line(point, detail_limit)}")
        if remaining > 0:
            lines.append(f"另有 {remaining} 个失败点，见完整报告")

    lines.append("选项：")
    for index, option in enumerate(RECOMMENDATIONS, start=1):
        label = RECOMMENDATION_LABELS.get(option, option)
        consequence = RECOMMENDATION_CONSEQUENCES.get(option, "")
        line = f"{index}. {_option_name(label)}（{option}）"
        if consequence:
            line += f"—— {consequence}"
        lines.append(line)

    lines.append("建议：（本次没有生成分析，无推荐）")

    suffix = f"（分析未生成：{analysis_error}）" if analysis_error else "（分析未生成）"
    lines.append(f"完整分析：{report_path}{suffix}")

    return "\n".join(lines)


def render_fallback_brief(
    *,
    plan_id: str = "",
    round_number: int = 0,
    stop_reason: str = "",
    points: Sequence[Any] = (),
    report_path: str = "",
    analysis_error: str = "",
    limit: int = TERMINAL_FALLBACK_BRIEF_MAX_CHARS,
) -> str:
    """Render the card for a round that stopped **without** an analysis.

    When the judge produced no analysis — no answer at all, or an
    answer that failed the contract — there is nothing for
    :func:`render_brief` to render, and the human used to be asked a
    bare sentence of facts that named no failure point.  This is the
    card that replaces it: the same decision shape as the analysis
    card, with the failed points spelled out by hand.

    **The cut order is the argument order.**  When the card is too
    long, the *actual result* is cut first — it is the one part of a
    point that is evidence rather than a fact about the failure, and a
    shorter evidence still names the failure.  Only then is a failed
    point dropped, and the ones that are dropped are **counted** on
    the card rather than lost.  The heading, the statuses, the option
    lines, the advice and the report path are cut **not at all**:
    they are what the human acts on, and a card that omits them asks
    for a decision nobody can make.

    The returned string is never longer than ``limit``, and never has
    more than :data:`TERMINAL_FALLBACK_BRIEF_MAX_LINES` lines — unless
    the parts that are never cut do not fit on their own, in which case
    the smallest card this function can write is returned rather than
    no card at all: a human still has to be asked something.

    Args:
        plan_id: The plan the round belongs to.
        round_number: The round that stopped.
        stop_reason: Why the round stopped.
        points: The failed points — mappings carrying ``id``,
            ``status``, ``title`` and ``actual_result``.
        report_path: Where the full report was written.
        analysis_error: Why there is no analysis — ``analysis_empty``,
            or ``analysis_rejected:<reason>``.
        limit: The cap, in characters.

    Returns:
        The card.  It is never empty: a round that stopped without an
        analysis is still a round somebody has to decide about.
    """
    try:
        max_chars = int(limit)
    except (TypeError, ValueError):
        return ""
    if max_chars <= 0:
        return ""

    cleaned: List[Dict[str, str]] = []
    for record in points or ():
        point = _fallback_point(record)
        if any(point.values()):
            # A point with nothing in it says nothing; a card that
            # counts it would promise a failure that is not there.
            cleaned.append(point)

    total = len(cleaned)
    max_shown = min(TERMINAL_FALLBACK_MAX_POINTS, total)
    min_shown = 1 if total else 0

    def build(shown: int, detail_limit: Optional[int]) -> str:
        return _render_fallback_card(
            plan_id=_one_line(_text(plan_id)),
            round_number=_round_number(round_number),
            stop_reason=_one_line(_text(stop_reason)),
            points=cleaned[:shown],
            remaining=total - shown,
            report_path=_one_line(_text(report_path)),
            analysis_error=_one_line(_text(analysis_error)),
            detail_limit=detail_limit,
        )

    def fits(card: str) -> bool:
        return (
            len(card) <= max_chars
            and len(card.splitlines()) <= TERMINAL_FALLBACK_BRIEF_MAX_LINES
        )

    for shown in range(max_shown, min_shown - 1, -1):
        whole = build(shown, None)
        if fits(whole):
            return whole

        # The details are cut to whatever fits: the longest detail is
        # the upper bound, and the card only gets shorter as the
        # budget grows, so the largest fitting budget is found by
        # bisection.
        longest = max(
            (
                len(_first_sentence(point["actual_result"]))
                for point in cleaned[:shown]
            ),
            default=0,
        )
        low, high = 0, longest
        best: Optional[str] = None
        while low <= high:
            middle = (low + high) // 2
            candidate = build(shown, middle)
            if fits(candidate):
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        if best is not None:
            return best

    # Nothing fits: the points are cut to the bone and one is kept.
    # The card is returned anyway — a human has to be asked something.
    return build(min_shown, 0)


# ---------------------------------------------------------------------------
# The judge
# ---------------------------------------------------------------------------


class TerminalDecisionJudge:
    """Renders a failed verification round into an analysis prompt.

    The judge is constructed with the coding tool it should use, which
    is what makes it testable: the unit tests hand it a recorder and
    assert on the shape of the single call.  Without a tool it builds a
    :class:`coding_tool.ClaudeCodingTool` on first use, on the
    ``verification`` scene — the same scene the verification path itself
    runs on.

    The judge investigates with the **read-only** tools
    (:data:`READ_ONLY_TOOLS`): it may read the chain record, the
    root-cause reports, the repair tasks and the code, and it may not
    write anything.
    """

    #: How long one analysis call may take.
    #:
    #: 600, not 300 (2026-11-06): the call now carries read-only tools,
    #: so the model may spend several rounds reading files and running
    #: commands before it answers.  The old budget was sized for a
    #: single read-and-answer over a prompt that held every fact.
    TIMEOUT_SECONDS = 600

    def __init__(
        self,
        coding_tool: Any = None,
        timeout_seconds: Optional[int] = None,
    ) -> None:
        self._coding_tool = coding_tool
        self._timeout = (
            int(timeout_seconds) if timeout_seconds else self.TIMEOUT_SECONDS
        )
        # The session the last call ran in, and the id of that session.
        # Both start empty: a judge that has not called anything yet has
        # no session, and saying so is better than inheriting one.
        self.last_session: Optional["JsonSession"] = None
        self.last_session_id: Optional[str] = None
        # Why the last answer is missing — "" when there is an answer.
        #
        # The judge swallows the reason for a bad answer on purpose:
        # the caller must not depend on it, and a failure to analyse is
        # not a failure of the round.  But the card that replaces the
        # analysis has to *name* the reason — "the provider call
        # raised" and "the evidence was not a list of strings" ask a
        # human different questions — so the judge keeps the reason
        # where the orchestrator can read it and carry it over.
        self.last_analysis_error: str = ""

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
            "你是验证轮终局的失败分析助手。一次验证轮没能通过，循环停下来，"
            "接下来怎么办要由人决定。\n\n"
            "你有只读调查工具（Bash / Read / Grep / Glob）："
            "你可以读文件、搜代码、跑只读命令去查证事实。"
            "READ ONLY：不要修改任何文件、不要写文件、不要提交、"
            "不要执行会改变工作区的命令。你的工作只是查证和判断。\n\n"
            "HARD RULES — 违反任何一条，这个回答会被丢弃：\n"
            "1. DO NOT FABRICATE. 不许编造事实。你没有真正读到过的失败原因、"
            "代码、日志，都不许当成确定结论；凡是写进答案的事实，"
            "都要指得出它的出处（哪个文件、哪条命令、哪一行）。\n"
            "2. INVESTIGATE. prompt 给的是摘要，不是全貌。"
            "凡是摘要里没有的事实，你必须自己去读："
            "根因链状态文件、根因报告、修复任务记录、实现轮记录、代码。"
            "没读就下结论，等于没查。\n"
            "3. NO \"无法确定\". 事实不足的时候，禁止只写「无法确定」。"
            "必须写清楚：**缺哪一条具体事实**、**去哪个文件或跑哪条命令能读到它**。"
            "uncertainty 字段写的是「还差什么、怎么取」，不是「不知道」。\n"
            "4. recommendation 只能是以下两者之一，不许自创第三种：\n"
            f"   - rerun_this_round：{RECOMMENDATION_LABELS['rerun_this_round']}"
            "（环境偶发失败、重跑一轮可能就过）。\n"
            f"   - revise_criterion：{RECOMMENDATION_LABELS['revise_criterion']}"
            "（判据本身有问题，不是交付物有问题：这一轮的失败判据要由人重写）。\n"
            "5. 全部用中文回答。字段名（context / problem / evidence / "
            "recommendation / recommendation_reason / uncertainty）和 "
            "recommendation 的取值保持英文原样，其余内容一律中文。\n"
            "6. 只输出 JSON，不要输出解释性文字，不要用 markdown 代码块包裹。"
            "六个字段一个都不许少。\n"
            "7. context 只写一句，recommendation_reason 只写一句。"
            "这两个字段会被直接贴到给人看的决策卡上，"
            "写长了卡片就变成一堵墙。"
            "逐 VP 的细节写进 problem / evidence / uncertainty 三个字段，"
            "那两个字段里写完整。\n"
            "8. evidence 必须是字符串数组：JSON 数组，元素是字符串。"
            "只找到一条证据时也写成单元素数组（[\"…\"]），"
            "不要写成一段话 —— 一段话不是数组。"
        )

    def build_prompt(
        self,
        *,
        plan_id: str,
        round_number: int,
        stop_reason: str,
        repair_task_count: int,
        deferred_count: int,
        failed_points: Any,
        plan_dir: str = "",
        project_dir: str = "",
    ) -> str:
        """The per-call half of the prompt: the scene, the answers, the
        output contract.

        The scene is rendered, not summarised: every failed point is
        named, the round-level facts carry their numbers, the long fields
        are cut and the cut is marked, and the points that did not fit
        are counted out loud.

        The prompt also carries **where to look** (``plan_dir`` and
        ``project_dir``) and **what to answer** — because a judge that
        is handed a summary and nothing else can only repeat the summary
        back.  The investigation guide names the files that hold the
        facts the summary does not, and the answer format names the
        three things a human reading the result needs: what each
        verification point expected and got, why the framework could not
        iterate on its own, and what to do next.
        """
        parts: List[str] = [
            "# 验证轮终局分析请求",
            "",
            "## 本轮现场事实",
        ]

        round_facts: Tuple[Tuple[str, str, str], ...] = (
            ("plan_id", _text(plan_id), ""),
            ("round_number", _text(round_number), ""),
            ("stop_reason", _text(stop_reason), ""),
            (
                "repair_task_count",
                _text(repair_task_count),
                f"repair tasks generated this round: {_text(repair_task_count)}",
            ),
            (
                "deferred_count",
                _text(deferred_count),
                f"deferred / not-executed points: {_text(deferred_count)}",
            ),
        )
        for label, value, gloss in round_facts:
            if not value:
                # Missing or None: the line renders as an empty string.
                continue
            if gloss:
                parts.append(f"- {label}: {value}  ({gloss})")
            else:
                parts.append(f"- {label}: {value}")

        parts.extend(["", "## 失败验证点"])
        points = list(failed_points) if failed_points else []
        if not points:
            parts.append("(no failed points recorded)")
        else:
            shown = points[:MAX_FAILED_POINTS]
            for point in shown:
                parts.extend(_render_point(point))
                parts.append("")
            hidden = len(points) - len(shown)
            if hidden > 0:
                parts.append(f"({hidden} more failed point(s) not shown)")

        parts.extend(["", "## 可选动作（recommendation 只能二选一）"])
        for recommendation in RECOMMENDATIONS:
            parts.append(
                f"- {recommendation}: {RECOMMENDATION_LABELS[recommendation]}"
            )

        parts.extend(self._investigation_section(plan_dir, project_dir))
        parts.extend(self._answer_requirements_section())

        parts.extend(
            [
                "",
                "## 输出要求",
                "evidence 必须是字符串数组：只有一条证据时也写成单元素数组"
                '（["<支撑判断的事实，中文>"]），不要写成一段话。',
                "只输出下面这个 JSON 对象，不要解释、不要 markdown：",
                '{"context": "<背景：这一轮停在哪里，一句话，中文>", "problem": '
                '"<问题：到底出了什么问题，逐 VP 的预期/实际/为何失败，中文>", '
                '"evidence": '
                '["<支撑判断的事实，中文>"], "recommendation": '
                '"<rerun_this_round|revise_criterion>", '
                '"recommendation_reason": "<为什么是这个建议，一句话，中文>", '
                '"uncertainty": "<还缺哪一条事实、去哪读，中文>"}',
            ]
        )
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # The investigation guide
    # ------------------------------------------------------------------

    def _investigation_section(
        self, plan_dir: str, project_dir: str,
    ) -> List[str]:
        """Where the judge may look, and what is in there.

        The paths are the ones the caller knows — the plan's own
        directory and the project it verifies.  The file names are the
        ones the verification loop actually writes, so a judge that
        opens one of them finds the record it was told to look for
        rather than an empty directory.
        """
        plan = _text(plan_dir)
        project = _text(project_dir)
        parts: List[str] = [
            "",
            "## 调查路径（只读）",
            f"- plan_dir: {plan or '(未提供)'}",
            f"- project_dir: {project or '(未提供)'}",
            "",
            "你可以（也必须）自己去读这些地方，不要只看上面的摘要：",
        ]
        if plan:
            parts.extend(
                [
                    f"- {plan}/verification_root_cause_chain.json"
                    " —— 根因链状态：每个 VP 的 stage、round、"
                    "implementation_round、blocked_round、blocked_reason",
                    f"- {plan}/reports/root-cause-<vp_id>.md"
                    " —— 每个 VP 的根因报告（诊断结论与要改的文件）",
                    f"- {plan}/verification_repair_tasks.json"
                    " —— 修复任务记录（本轮修了什么、修了没有）",
                    f"- {plan}/verification_report.json"
                    " —— 本轮验证报告（每个 VP 的完整结果）",
                ]
            )
        if project:
            parts.append(
                f"- {project}/ 下的实现代码"
                "（用 Read / Grep / Glob 定位被改的那部分）"
            )
        parts.append(
            "- 用 Bash 跑只读命令查证（ls / cat / grep / git log / "
            "git show），但绝不允许修改文件、提交代码或改动工作区。"
        )
        return parts

    def _answer_requirements_section(self) -> List[str]:
        """The three things the answer has to contain.

        Each one is a thing the summary alone cannot answer: the per
        point expectation-versus-actual, the reason the framework could
        not iterate, and the actionable next step.
        """
        return [
            "",
            "## 必须回答的问题（缺一不可）",
            "",
            "1. **逐 VP 结论**。每个失败点都要有一行 `id — title`，"
            "并在该行下面逐点写清三行，格式固定：",
            "   ```",
            "   VP-3 — 登录接口",
            "     预期: <该点的 expected_result>",
            "     实际: <该点的 verdict>",
            "     为何失败: <一句话，引用你读到的证据>",
            "   ```",
            "   失败点有几个就写几组，不许只写其中一个、"
            "也不许用「其余类似」代替。"
            "上面若写了「more failed point(s) not shown」，"
            "就去 <plan_dir>/verification_report.json 读完整列表，"
            "被砍掉的那些也要写。",
            "",
            "2. **框架为什么不能自动迭代**。这必须靠你读文件得到，"
            "不能从摘要里猜：",
            "   - 读根因链文件"
            "（<plan_dir>/verification_root_cause_chain.json），"
            "看链停在哪一步；",
            "   - 读根因报告（<plan_dir>/reports/root-cause-*.md）"
            "与修复任务记录（<plan_dir>/verification_repair_tasks.json）；",
            "   - 如果链停在某个 VP 的 BLOCKED_ON_USER，"
            "再读这个 VP 的根因报告和它的实现轮记录"
            "（链里的 implementation_round / blocked_round / blocked_reason），"
            "回答它**为什么 block**：是「已经实现过但没修好」、"
            "「环境敏感/环境本身挡住了」、还是「需要方向性决策、人来定」；",
            "   - 如果 repair_task_count=0，要说明这个 0 的机制："
            "链已经 terminal 时，check_cycle_conditions 在生成修复任务之前"
            "就返回了空表，所以这一轮没有任务可以自动派发 —— "
            "说清楚「不是没人看见，是这条路已经走完」。",
            "",
            "3. **可执行的下一步**。不要只写「人工安排」「建议人工介入」："
            "要写清楚改哪个文件（或哪一类行为）、为什么改它、"
            "以及谁来做（实现 agent / 验证 agent / 人）。",
            "",
            "## 禁止「无法确定」",
            "事实不足的时候，禁止只写「无法确定」「信息不足」「有待确认」。"
            "必须写成：**缺哪一条具体事实** + "
            "**去哪个文件读或跑哪条命令能读到**。"
            "例如「缺少 VP-3 最近一次实现的 diff："
            "读 <project_dir> 下的 git log --oneline -- <文件路径>」。",
        ]

    # ------------------------------------------------------------------
    # The call
    # ------------------------------------------------------------------

    def analyze(
        self,
        *,
        plan_id: str,
        round_number: int,
        stop_reason: str,
        repair_task_count: int,
        deferred_count: int,
        failed_points: Any,
        plan_dir: str = "",
        project_dir: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Read the failure scene and say what should happen next.

        One call, one answer.  The call carries the **read-only**
        investigation tools (:data:`READ_ONLY_TOOLS`), so the judge can
        go and read the chain record, the root-cause reports and the
        code instead of answering from the summary alone.  Everything
        that could make the answer unusable — an exception from the
        provider, an answer that is not a JSON object, a recommendation
        outside the three actions the question offers, a field that is
        missing — produces ``None``.

        ``None`` means **"no analysis"**, not "the analysis found
        nothing": the caller falls back to the factual question, so a
        human is always asked something.

        Args:
            plan_id: The plan the round stopped in.
            round_number: Which round of the verification loop stopped.
            stop_reason: Why the loop stopped.
            repair_task_count: How many repair tasks the round produced.
                ``0`` is a load-bearing fact: it says the automatic path
                had no next step.
            deferred_count: How many points were deferred or never
                executed.
            failed_points: The failed points — each with ``id``,
                ``title``, ``expected_result``, ``failure_class`` and
                ``verdict``.
            plan_dir: The plan's own directory — where the chain record,
                the root-cause reports and the repair tasks live.  It
                goes into the prompt so the judge can read them.
            project_dir: The project the plan verifies — where the
                implementation lives.  It goes into the prompt for the
                same reason.

        Returns:
            The analysis as a dict with ``context``, ``problem``,
            ``evidence``, ``recommendation``, ``recommendation_reason``
            and ``uncertainty`` — or ``None`` when there is none.
            Never raises.

        The call runs inside a session handle, so the id of the session
        that produced the answer is kept on the judge
        (:attr:`last_session_id`) — the execution phase can then continue
        the same conversation instead of starting a new one.  The id is
        recorded *before* the answer is judged: an answer that breaks the
        contract still came out of a session, and re-asking inside that
        session is what makes sense.
        """
        # Every call starts from nothing.  An id left over from the last
        # answer names a *previous* conversation, and resuming that one
        # would be worse than resuming none at all.
        self.last_session = None
        self.last_session_id = None
        # The reason for a missing answer belongs to *this* call: a
        # refusal left over from the previous one would name a check
        # that has nothing to do with the answer at hand.
        self.last_analysis_error = ""

        try:
            from coding_tool import JsonSession, query_json_in_session

            handle = JsonSession()
            response = query_json_in_session(
                self.coding_tool,
                handle,
                prompt=self.build_prompt(
                    plan_id=plan_id,
                    round_number=round_number,
                    stop_reason=stop_reason,
                    repair_task_count=repair_task_count,
                    deferred_count=deferred_count,
                    failed_points=failed_points,
                    plan_dir=plan_dir,
                    project_dir=project_dir,
                ),
                system_instruction=self.build_system_prompt(),
                timeout=self._timeout,
                allowed_tools=list(READ_ONLY_TOOLS),
            )
        except Exception as exc:  # noqa: BLE001 — the judge never raises
            logger.warning(
                "[terminal-analysis] LLM call failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            self.last_analysis_error = ANALYSIS_ERROR_EMPTY
            return None

        self.last_session = handle
        self.last_session_id = handle.session_id

        payload = _parse_payload(response)
        if payload is None:
            logger.warning(
                "[terminal-analysis] the answer is not a JSON object"
            )
            self.last_analysis_error = ANALYSIS_ERROR_EMPTY
            return None

        analysis, reason = validate_analysis_with_reason(payload)
        if analysis is None:
            logger.warning(
                "[terminal-analysis] the answer does not carry the six "
                "contracted fields: %s",
                reason,
            )
            self.last_analysis_error = ANALYSIS_ERROR_REJECTED + reason
            return None

        logger.info(
            "[terminal-analysis] plan %s round %s → %s (%d evidence)",
            plan_id,
            round_number,
            analysis["recommendation"],
            len(analysis["evidence"]),
        )
        return analysis
