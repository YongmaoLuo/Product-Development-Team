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
scene into a prompt, makes **one** tool-less call with it, and reads the
answer back under a strict contract.  It is deliberately a separate
module with no tools and no orchestrator import, so that

* the rendering can be tested on its own — every fact, every cut, every
  bound is asserted without an LLM in the loop;
* the orchestrator, which will import *this* module, is not imported
  *by* it — an import the other way round would be a cycle;
* the model behind the ask cannot act on the scene it is shown.  It gets
  no tools, so it can only read what the loop already recorded.

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

**The recommendation vocabulary is the orchestrator's.**  This module
does not import the orchestrator (the orchestrator will import *this*
module), so the two lists of recommendations can drift apart unless
something pins them together.  ``tests/unit/test_terminal_analysis_judge.py``
is that something.

What the call guarantees
========================

**One call, no tools.**  The judge is a reader, not an investigator: it
is shown what the loop already recorded and asked what that means.  A
judge with tools would go and look at the repository instead of reading
the scene it was given.

**The answer is accepted whole or not at all.**  Six fields —
``context``, ``problem``, ``evidence``, ``recommendation``,
``recommendation_reason``, ``uncertainty``.  An answer that is missing
one, or whose recommendation is not one of the actions, is
rejected whole rather than half-used: a half-analysis in front of a
human is worse than none, because it reads as an analysis.

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
Only then is the text cut, and the cut is marked with ``…``.

**An analysis that is not one renders as nothing.**  Not a mapping, a
missing field, a recommendation outside the actions — all of them
produce ``""``, and the caller's fallback is the factual question it
asked before.

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
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only
    from coding_tool import JsonSession

__all__ = [
    "MAX_EXPECTED_RESULT_CHARS",
    "MAX_FAILED_POINTS",
    "MAX_REASON_CHARS",
    "RECOMMENDATIONS",
    "RECOMMENDATION_LABELS",
    "TERMINAL_QUESTION_MAX_CHARS",
    "TerminalDecisionJudge",
    "render_analysis",
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
    """
    recommendation = payload.get("recommendation")
    if not isinstance(recommendation, str):
        return None
    recommendation = recommendation.strip()
    if recommendation not in RECOMMENDATIONS:
        return None

    text_fields: Dict[str, str] = {}
    for name in _ANALYSIS_TEXT_FIELDS:
        value = payload.get(name)
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            # Present but empty: a section that says nothing is a
            # section that is not there.
            return None
        text_fields[name] = text

    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, (list, tuple)) or not raw_evidence:
        return None
    evidence: List[str] = []
    for item in raw_evidence:
        if not isinstance(item, str):
            return None
        text = item.strip()
        if not text:
            return None
        evidence.append(text)

    return {
        "context": text_fields["context"],
        "problem": text_fields["problem"],
        "evidence": evidence,
        "recommendation": recommendation,
        "recommendation_reason": text_fields["recommendation_reason"],
        "uncertainty": text_fields["uncertainty"],
    }


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
    not said, and half of one is a fact.  Only when that was not enough
    either is the text cut, with the cut marked by ``…``.

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

    def build(items: List[str], keep_uncertainty: bool) -> str:
        return _render_cpea(
            context=context,
            problem=problem,
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

    # 3. The hard cut, marked so that it does not read as a complete text.
    return rendered[: max_chars - 1] + "…"


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
    """

    #: How long one analysis call may take.
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
        # The session the last call ran in, and the id of that session.
        # Both start empty: a judge that has not called anything yet has
        # no session, and saying so is better than inheriting one.
        self.last_session: Optional["JsonSession"] = None
        self.last_session_id: Optional[str] = None

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
            "接下来怎么办要由人决定。你要读的是下面 prompt 里给出的失败现场，"
            "然后给出一个 recommendation（建议）。\n\n"
            "NO TOOLS. 你没有任何工具：不读写文件、不执行命令、不访问网络。"
            "你唯一的输入就是 prompt 里给出的事实，唯一的工作就是分析它们。\n\n"
            "HARD RULES — 违反任何一条，这个回答会被丢弃：\n"
            "1. DO NOT FABRICATE. 不许编造事实。prompt 没有给出的失败原因、"
            "代码、日志都不许当成确定结论；证据不足就在 uncertainty 里说清楚。\n"
            "2. recommendation 只能是以下两者之一，不许自创第三种：\n"
            f"   - rerun_this_round：{RECOMMENDATION_LABELS['rerun_this_round']}"
            "（环境偶发失败、重跑一轮可能就过）。\n"
            f"   - revise_criterion：{RECOMMENDATION_LABELS['revise_criterion']}"
            "（判据本身有问题，不是交付物有问题：这一轮的失败判据要由人重写）。\n"
            "3. 全部用中文回答。字段名（context / problem / evidence / "
            "recommendation / recommendation_reason / uncertainty）和 "
            "recommendation 的取值保持英文原样，其余内容一律中文。\n"
            "4. 只输出 JSON，不要输出解释性文字，不要用 markdown 代码块包裹。"
            "六个字段一个都不许少。"
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
    ) -> str:
        """The per-call half of the prompt: the scene, the answers, the
        output contract.

        The scene is rendered, not summarised: every failed point is
        named, the round-level facts carry their numbers, the long fields
        are cut and the cut is marked, and the points that did not fit
        are counted out loud.
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

        parts.extend(
            [
                "",
                "## 输出要求",
                "只输出下面这个 JSON 对象，不要解释、不要 markdown：",
                '{"context": "<背景：这一轮停在哪里，中文>", "problem": '
                '"<问题：到底出了什么问题，中文>", "evidence": '
                '["<支撑判断的事实，中文>"], "recommendation": '
                '"<rerun_this_round|revise_criterion>", '
                '"recommendation_reason": "<为什么是这个建议，中文>", '
                '"uncertainty": "<证据不足或需要人来判断的地方，中文>"}',
            ]
        )
        return "\n".join(parts)

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
    ) -> Optional[Dict[str, Any]]:
        """Read the failure scene and say what should happen next.

        One call, no tools, one answer.  Everything that could make the
        answer unusable — an exception from the provider, an answer that
        is not a JSON object, a recommendation outside the actions
        the question offers, a field that is missing — produces
        ``None``.

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
                ),
                system_instruction=self.build_system_prompt(),
                timeout=self._timeout,
                allowed_tools=[],
            )
        except Exception as exc:  # noqa: BLE001 — the judge never raises
            logger.warning(
                "[terminal-analysis] LLM call failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return None

        self.last_session = handle
        self.last_session_id = handle.session_id

        payload = _parse_payload(response)
        if payload is None:
            logger.warning(
                "[terminal-analysis] the answer is not a JSON object"
            )
            return None

        analysis = _validate_analysis(payload)
        if analysis is None:
            logger.warning(
                "[terminal-analysis] the answer does not carry the six "
                "contracted fields"
            )
            return None

        logger.info(
            "[terminal-analysis] plan %s round %s → %s (%d evidence)",
            plan_id,
            round_number,
            analysis["recommendation"],
            len(analysis["evidence"]),
        )
        return analysis
