"""Four-class failure triage for the HITL verification path (P0-2).

Why this module exists
======================

P0-2 requires every verification verdict to carry the *nature* of its
failure. Until now the only thing downstream could see was the fact of
it — one bit — and every consumer had to guess what to do about it. The
HITL loop needs four different answers to "it failed":

``infra``
    The environment failed, not the work: a rate limit, a timeout, a
    watchdog kill, an answer that could not be parsed. The self-heal
    loop already calls these "attempt malfunctions"
    (``verification_subagent.py``: ``VerdictParseError``,
    ``WatchdogKilledError``, transport error) and already spends its
    retry budget on exactly those. Retrying is the only sensible
    response, and the attempt must NOT count against the deliverable.
``deliverable_defect``
    The deliverable under verification is genuinely wrong. The work has
    to be redone, and the attempt DOES count — this is the failure a
    human is meant to see. Tier A does not name it: the eight
    deterministic signals the decision point pins belong to the other
    three classes, so this one is what the semantic tier — or the
    conservative default — reaches.
``criterion_defect``
    The criterion is wrong: the verification point cannot be expressed
    (a schema rejection), cannot be run as written (the request method
    is not one the runner speaks), or collects nothing at all (the
    ``--collect-only`` run comes back empty). No amount of reworking
    the deliverable will ever satisfy it, so the loop must escalate to
    a human. It counts: repeating it IS the same-cause loop the
    escalation exists to break.
``out_of_scope``
    The check reaches outside what was agreed: the diff touches a path
    the task never declared. Skip it, and do not count it.

The classification is therefore not a label. It decides whether a
human is interrupted, whether the deliverable is reworked, and whether
the same-cause counter moves — three consequences that send the loop in
opposite directions depending on the class.

The tier boundary
=================

This module is the **deterministic tier** (tier A): pure rules, no I/O,
no model call, no clock. A signal it does not know returns ``None``
rather than a guess, and the caller hands the case to the semantic
tier (tier B). That "return ``None`` instead of guessing" is load-
bearing: a rule table that guesses is a rule table whose answers drift
whenever a new error text shows up, and the whole point of tier A is
that the answer does not depend on who is looking.

The semantic tier and its anchor
================================

Tier B is the model: it reads the raw output and names the nature of
the failure. A model's answer is only worth acting on when it points
at something, so tier B has to carry the fragment it based that
answer on (``evidence_quote``), and that fragment has to appear
**verbatim** in the raw output (``actual_result``). A quote that
matches only after whitespace, case or Unicode normalisation is a
paraphrase, and a paraphrase is not an anchor — so
:func:`validate_evidence_quote` is deliberately strict, and
:func:`classify_verdict` degrades an unanchored answer to
``deliverable_defect`` rather than letting it through.

The degradation always lands on the class that *counts*. That is not
an accident: the other three classes either skip the attempt or do
not count it, so a verdict nobody could verify must not quietly
become one of them — a wrong "the environment broke" is how a broken
deliverable gets retried forever without a human ever seeing it.

What this module deliberately does NOT do
==========================================

It does not touch ``backend/failure_classification.py``. That module
owns the *refiner split gate* — a different question (is this the same
failure twice, so the task should be split?) with a different set of
kinds and a different consumer. Re-using it here would couple the
split gate to the HITL loop: a change to one would silently change the
other, and the two gates have opposite dispositions for the same
underlying event (a timeout is "do not split" there and "retry, do not
count" here). This module is a separate, self-contained base.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


class FailureClass:
    """The four classes a verification verdict can carry.

    Plain string constants rather than an ``Enum``: the class travels
    through JSON verdict records and through database columns, and a
    plain ``str`` compares equal to its literal at every boundary
    without a coercion step. ``FailureClass.INFRA == "infra"`` holds,
    which is what the verdict records and the decision keys carry.
    """

    #: The environment failed, not the work.
    INFRA = "infra"
    #: The deliverable under verification is genuinely wrong.
    DELIVERABLE_DEFECT = "deliverable_defect"
    #: The criterion (the verification point) is wrong.
    CRITERION_DEFECT = "criterion_defect"
    #: The check asks for something outside the agreed scope.
    OUT_OF_SCOPE = "out_of_scope"


#: Module-level aliases, so the class reads the way the sibling module
#: ``failure_classification`` is imported elsewhere in the backend.
INFRA = FailureClass.INFRA
DELIVERABLE_DEFECT = FailureClass.DELIVERABLE_DEFECT
CRITERION_DEFECT = FailureClass.CRITERION_DEFECT
OUT_OF_SCOPE = FailureClass.OUT_OF_SCOPE

#: Every class, in the order the disposition table lists them.
ALL_FAILURE_CLASSES: Tuple[str, ...] = (
    INFRA,
    DELIVERABLE_DEFECT,
    CRITERION_DEFECT,
    OUT_OF_SCOPE,
)


class Disposition:
    """What the HITL loop does with a verdict of each class.

    The name carries both halves of the decision: the action, and
    whether the attempt counts against the same-cause counter. Spelling
    the two together is deliberate — a table that listed the action in
    one place and the counting rule in another is how the two drift
    apart, and a verdict that is retried but counted burns the counter
    on an environment blip.
    """

    #: Transient environment failure: retry it, and do not count it.
    RETRY_NOT_COUNTED = "retry_not_counted"
    #: The deliverable is wrong: rework it, and count it.
    RETRY_COUNTED = "retry_counted"
    #: The criterion is wrong: a human has to fix it, and it counts.
    ESCALATE_COUNTED = "escalate_counted"
    #: The check is out of scope: skip it, and do not count it.
    SKIP_NOT_COUNTED = "skip_not_counted"


#: The class -> disposition table. The single source of truth for both
#: :func:`disposition_for` and the membership test behind
#: :func:`is_counted`.
_DISPOSITIONS: Dict[str, str] = {
    INFRA: Disposition.RETRY_NOT_COUNTED,
    DELIVERABLE_DEFECT: Disposition.RETRY_COUNTED,
    CRITERION_DEFECT: Disposition.ESCALATE_COUNTED,
    OUT_OF_SCOPE: Disposition.SKIP_NOT_COUNTED,
}

#: The classes whose failure moves the same-cause counter. Exactly the
#: classes whose disposition does NOT say "not counted". The negative
#: form is the one tested for, because ``"retry_not_counted"`` also
#: ends in ``"_counted"`` — testing for the positive form alone
#: counts the infrastructure failures.
_COUNTED_CLASSES = frozenset(
    failure_class
    for failure_class, disposition in _DISPOSITIONS.items()
    if not disposition.endswith("_not_counted")
)

#: The source tag every classification made by this module carries. The
#: deterministic tier is tier A, so a class returned from here is
#: ``deterministic`` by construction; the semantic tier owns the cases
#: this module returns ``None`` for.
SOURCE_DETERMINISTIC = "deterministic"


# ---------------------------------------------------------------------------
# Tier A — the deterministic signal table
# ---------------------------------------------------------------------------
#
# The canonical signals, with the aliases the producers actually use.
# The aliases are not decoration: a caller that spells the same event
# two ways must not get two different verdicts, and the cost of an
# unknown spelling is a ``None``, which sends the case to the semantic
# tier for no reason.
_DETERMINISTIC_SIGNALS: Dict[str, str] = {
    # --- infra: the attempt malfunctioned, the work is not implicated --
    "parse_error": INFRA,
    "json_parse_error": INFRA,
    "verdict_parse_error": INFRA,
    "malformed_output": INFRA,
    "rate_limited": INFRA,
    "rate_limit": INFRA,
    "http_429": INFRA,
    "too_many_requests": INFRA,
    "transport_error": INFRA,
    "timeout": INFRA,
    "timed_out": INFRA,
    "hard_timeout": INFRA,
    "watchdog_kill": INFRA,
    "watchdog": INFRA,
    "watchdog_timeout": INFRA,
    # 2026-10-24: the request never reached the service. These are the
    # names ``verification_api_runner`` emits for a transport failure and
    # the names the api_test guard emits for a service it could not
    # bring up. They are listed here so tier A decides them by rule:
    # an unknown signal is a ``None``, and a ``None`` is handed to the
    # semantic tier, which lands on ``deliverable_defect`` — the class
    # that counts against the same-cause threshold.
    "connection_refused": INFRA,
    "connection_reset": INFRA,
    "dns_error": INFRA,
    "name_resolution_error": INFRA,
    "service_not_ready": INFRA,
    "blocked_ports": INFRA,
    "service_unavailable": INFRA,
    # --- criterion_defect: the verification point cannot be run -------
    "schema_rejected": CRITERION_DEFECT,
    "schema_violation": CRITERION_DEFECT,
    "schema_error": CRITERION_DEFECT,
    "method_mismatch": CRITERION_DEFECT,
    "unsupported_method": CRITERION_DEFECT,
    "method_not_supported": CRITERION_DEFECT,
    # The check collects nothing: it cannot be run as written, so
    # reworking the deliverable will never satisfy it.
    "zero_collected": CRITERION_DEFECT,
    "collected_zero": CRITERION_DEFECT,
    "no_tests_collected": CRITERION_DEFECT,
    # 2026-10-24: the verification point has **no definition at all** —
    # no title, no usable method, or no entry in the plan file. There is
    # nothing to verify, and the executor must not invent one: a
    # criterion that was never written down cannot be reworked into
    # existence by reworking the deliverable. The operator voids or
    # re-expresses the criterion. Same nature of failure as a schema
    # rejection — the criterion is what is broken.
    #
    # It has to be an **explicit** rule here: an unknown signal returns
    # ``None`` and is handed to the semantic tier, which lands on
    # ``deliverable_defect`` — the class that counts against the
    # same-cause threshold and that becomes a repair task. A missing
    # criterion is not a broken deliverable.
    "vp_definition_missing": CRITERION_DEFECT,
    # --- out_of_scope: the check reaches outside the agreed scope ----
    "diff_outside_path": OUT_OF_SCOPE,
    "undeclared_write": OUT_OF_SCOPE,
    "blocked_path": OUT_OF_SCOPE,
}

#: The text markers tier A falls back to when the caller has only the
#: raw error text and no signal name. Ordered most specific first:
#: "watchdog killed the subprocess" has to win over the plain
#: "timeout" pattern. The classes are the ones the signal table
#: carries — the text fallback must not route a failure somewhere the
#: named signal would not have.
_TEXT_MARKERS: Tuple[Tuple["re.Pattern[str]", str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), failure_class)
    for pattern, failure_class in (
        (r"\b429\b", INFRA),
        (r"too many requests", INFRA),
        (r"rate limit", INFRA),
        (r"timed out", INFRA),
        (r"\btimeout\b", INFRA),
        (r"watchdog", INFRA),
        (r"json decode error", INFRA),
        (r"expecting value", INFRA),
        (r"invalid json", INFRA),
        (r"parse error", INFRA),
        # 2026-10-24: the request never reached the service. These are
        # the words the transport layer actually produces — the raw
        # ``URLError`` text, the errno behind it, and the api_test
        # guard's own wording. They sit above the criterion-defect
        # markers on purpose: a transport failure must never be read as
        # a broken criterion.
        (r"connection refused", INFRA),
        (r"errno 61", INFRA),
        (r"connection reset", INFRA),
        (r"name or service not known", INFRA),
        (r"nodename nor servname", INFRA),
        (r"temporary failure in name resolution", INFRA),
        (r"service not ready", INFRA),
        (r"service_unavailable", INFRA),
        (r"blocked_ports", INFRA),
        (r"method must be one of", CRITERION_DEFECT),
        (r"unsupported method", CRITERION_DEFECT),
        (r"method not allowed", CRITERION_DEFECT),
        (r"schema violation", CRITERION_DEFECT),
        (r"schema error", CRITERION_DEFECT),
        (r"invalid schema", CRITERION_DEFECT),
        (r"schema:", CRITERION_DEFECT),
        (r"collected 0 items", CRITERION_DEFECT),
        (r"no tests ran", CRITERION_DEFECT),
        (r"no tests collected", CRITERION_DEFECT),
        (
            r"refusing to edit a file this task did not declare",
            OUT_OF_SCOPE,
        ),
        (r"undeclared_write", OUT_OF_SCOPE),
    )
)

#: The verdict literals the verification layer uses. A value from this
#: set is a verdict, never a failure class — the two vocabularies are
#: disjoint, and conflating them is the mistake this set prevents.
_VERDICT_TOKENS = frozenset({"PASSED", "FAILED", "SKIPPED", "BLOCKED"})


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def _normalize_signal(signal) -> str:
    """The canonical spelling of a signal name.

    Callers spell these events in their own dialect — ``"rate_limit"``,
    ``"Rate Limited"``, ``"RATE-LIMITED"`` — and the table must not
    care. Empty in, empty out: a missing signal is not a signal.
    """
    if signal is None:
        return ""
    text = re.sub(r"[\s\-]+", "_", str(signal).strip().lower())
    return text.strip("_")


def _normalize_failure_class(failure_class) -> str:
    """The canonical class for ``failure_class``.

    A missing or unrecognised class is read as ``deliverable_defect``.
    That is the only class whose verdict is safe to act on when nobody
    has said otherwise: the other three either do not count or demand
    a human, and reaching one of those by accident is the expensive
    mistake this default exists to prevent. A caller that knows better
    says so; a caller that knows nothing gets the conservative answer.
    """
    if failure_class is None:
        return DELIVERABLE_DEFECT
    key = re.sub(r"[\s\-]+", "_", str(failure_class).strip().lower()).strip("_")
    if key in _DISPOSITIONS:
        return key
    return DELIVERABLE_DEFECT


def _is_verdict_token(value) -> bool:
    """Whether ``value`` is a verdict literal rather than a class."""
    return isinstance(value, str) and value.strip().upper() in _VERDICT_TOKENS


def _is_passed(verdict) -> bool:
    """Whether ``verdict`` says the verification point passed."""
    if verdict is None:
        return False
    return str(verdict).strip().upper() == "PASSED"


def _classify_by_text(text) -> Optional[str]:
    """The class the raw error text points at, or ``None``.

    Still tier A: these are patterns, not a model call. A text that
    matches nothing returns ``None`` for the same reason an unknown
    signal does — the semantic tier exists for that case.
    """
    if not text:
        return None
    haystack = str(text)
    for pattern, failure_class in _TEXT_MARKERS:
        if pattern.search(haystack):
            return failure_class
    return None


def classify_deterministic(signal, text) -> Optional[str]:
    """Classify a deterministic (tier A) failure signal.

    Args:
        signal: The machine-readable signal name the caller already
            knows — ``"rate_limited"``, ``"schema_rejected"``, ...
            Empty when the caller has only the raw error text.
        text: The raw error text. Used as the fallback when no signal
            name was supplied, and carried for the caller's own
            diagnostics either way.

    Returns:
        One of the four :class:`FailureClass` literals, or ``None`` when
        this module has no rule for the case. ``None`` means "the
        deterministic tier has no opinion" — the caller hands the case
        to the semantic tier rather than acting on a guess.

        An *unknown* signal returns ``None`` even when the text would have
        matched a marker: a caller that knows which event it is looking
        at, and spelled it in a way this table does not carry, must be
        told "not mine" rather than quietly classified by its text.
    """
    key = _normalize_signal(signal)
    if key:
        return _DETERMINISTIC_SIGNALS.get(key)
    return _classify_by_text(text)


def decision_key(vp_id, failure_class=None, verdict=None) -> str:
    """The identity of one verification decision.

    Two failures are "the same failure" for the same-cause counter when
    they hit the same verification point *and* have the same nature. The
    key is that pair, joined by a pipe so it stays readable in a log
    line: ``"VP-007|criterion_defect"``.

    Args:
        vp_id: The verification point id, e.g. ``"VP-007"``.
        failure_class: The class of the failure. A missing or unknown
            class is read as ``deliverable_defect`` (see
            :func:`_normalize_failure_class`). A verdict literal in this
            position is read as the verdict instead — the two
            vocabularies are disjoint, and a caller holding only a
            verdict should not have to know that.
        verdict: The verdict of the attempt, when the caller has it.
            ``"PASSED"`` means there is no failure, so there is no
            decision to key: the result is the empty string.

    Returns:
        The decision key, or ``""`` for a passed verdict. An empty key
        is what the loop counts on: a point that once failed and later
        passed must never join a same-cause group, or it is still
        reported as "the same failure, again".
    """
    if verdict is None and _is_verdict_token(failure_class):
        verdict, failure_class = failure_class, None
    if _is_passed(verdict):
        return ""
    point = "" if vp_id is None else str(vp_id).strip()
    return f"{point}|{_normalize_failure_class(failure_class)}"


def disposition_for(failure_class) -> str:
    """What the loop does with a failure of this class.

    Args:
        failure_class: The class of the failure. A missing or unknown
            class is read as ``deliverable_defect``.

    Returns:
        One of the four :class:`Disposition` literals.
    """
    return _DISPOSITIONS[_normalize_failure_class(failure_class)]


def is_counted(failure_class) -> bool:
    """Whether a failure of this class moves the same-cause counter.

    Only the failures a human should see move it. An infrastructure
    blip is not evidence that the work is wrong, and an out-of-scope
    check is not evidence of anything; counting either would escalate a
    healthy run to a human and would make the counter report "same
    cause, three times" about failures that have nothing in common.

    Args:
        failure_class: The class of the failure. A missing or unknown
            class is read as ``deliverable_defect``, which counts.

    Returns:
        ``True`` when the attempt counts against the same-cause loop.
    """
    return _normalize_failure_class(failure_class) in _COUNTED_CLASSES


# ---------------------------------------------------------------------------
# Tier B — the semantic tier and its anchor
# ---------------------------------------------------------------------------
#
#: The source tag a classification carries when it came from the model
#: *and* the model anchored it in the raw output.
SOURCE_LLM = "llm"

#: The source tag a verdict carries when its classification could not
#: be verified and was therefore defaulted. A defaulted verdict is the
#: conservative one: it counts, it gets reworked, and it is the class
#: a human is meant to see.
SOURCE_DEFAULTED = "defaulted"


def validate_evidence_quote(quote, actual_result) -> bool:
    """Whether ``quote`` appears **verbatim** in ``actual_result``.

    A tier-B classification is only as good as the fragment of raw
    output it points at. The fragment has to be there, character for
    character: same case, same spacing, same Unicode form. A quote
    that matches only after normalising whitespace, folding case or
    re-composing Unicode is a *paraphrase* of the output, and a
    paraphrase is exactly what a model produces when it is describing
    what it saw instead of quoting it. Accepting those would turn the
    anchor into decoration and let any claim through.

    The nine ways a quote can relate to the text it claims to quote
    are therefore enumerable, and only two of them pass: the whole
    string, and a genuine substring of it. Everything else is a
    near-miss, and a near-miss is a failure.

    Args:
        quote: The fragment the classification was based on. A missing
            quote (``None``) or an empty one is not an anchor.
        actual_result: The raw output the fragment has to be found in.
            A missing result cannot anchor anything.

    Returns:
        ``True`` when the quote is a literal substring of the result,
        ``False`` in every other case — including the near-misses
        that would match after normalisation, and the all-whitespace
        quote, which contains no fragment to check at all.
    """
    if not isinstance(quote, str) or not isinstance(actual_result, str):
        return False
    if not quote.strip():
        # An empty (or all-whitespace) quote carries no fragment, so
        # there is nothing to verify. Treating it as a match would
        # hand every verdict that quotes nothing a free pass.
        return False
    # Plain substring semantics on purpose: no case folding, no
    # whitespace collapsing, no Unicode normalisation. ``str.__contains__
    # compares code points, so an NFC quote does not match an NFD
    # result, which is the point.
    return quote in actual_result


def _verdict_field(verdict, name, default=None):
    """Read one field of a verdict record.

    The record reaches this module in two shapes: a dataclass
    (``Verdict``) at the point where the verdict is produced, and a
    plain dict where it is projected for the report. Reading both
    here means the rule below does not have to know which one it was
    handed.
    """
    if verdict is None:
        return default
    if isinstance(verdict, Mapping):
        return verdict.get(name, default)
    return getattr(verdict, name, default)


def _claimed_failure_class(claimed) -> Optional[str]:
    """The canonical class a *claim* names, or ``None``.

    Unlike :func:`_normalize_failure_class` — which answers "what
    should we do with this class?" and therefore defaults an unknown
    one — this answers "did the claim name a class at all?". The
    difference is load-bearing for tier B: a claim outside the
    vocabulary was never earned, and an unearned claim must be
    degraded rather than silently read as ``deliverable_defect``
    *and kept*.
    """
    if claimed is None:
        return None
    key = re.sub(r"[\s\-]+", "_", str(claimed).strip().lower()).strip("_")
    if key in _DISPOSITIONS:
        return key
    return None


def classify_verdict(verdict) -> Tuple[str, str]:
    """Classify a verdict record, degrading an unanchored claim.

    Returns the pair ``(failure_class, failure_class_source)``.

    The record's own ``failure_class`` is a *claim*. It survives only
    when the claim is anchored: the ``evidence_quote`` it carries must
    appear verbatim in the ``actual_result`` (see
    :func:`validate_evidence_quote`). An unanchored claim is degraded
    to ``deliverable_defect`` with the source ``defaulted``.

    The degradation always lands on the class that counts (see
    :func:`is_counted`), and that is deliberate. The other three
    classes either skip the attempt (``out_of_scope``) or do not count
    it (``infra``), so letting an unverifiable claim through would be a
    way to file a broken deliverable as "the environment broke" —
    retried forever, never counted, never shown to a human.

    The deterministic tier needs no anchor and is not degraded: it was
    decided by a rule, so there is no quote to check, and a watchdog
    kill that got filed as a broken deliverable would be exactly the
    confusion the four classes exist to prevent.

    Args:
        verdict: The verdict record — a mapping or an object with the
            attributes ``verdict``, ``failure_class``,
            ``failure_class_source``, ``evidence_quote`` and
            ``actual_result``.

    Returns:
        ``(failure_class, failure_class_source)``. A verdict that
        passed has no failure to classify and returns ``("", "")``.
    """
    if _is_passed(_verdict_field(verdict, "verdict")):
        # A passed verdict has no failure. The empty class is the
        # convention the verdict record itself uses for that case.
        return ("", "")

    source = str(_verdict_field(verdict, "failure_class_source") or "").strip().lower()
    claimed = _verdict_field(verdict, "failure_class")

    if source == SOURCE_DETERMINISTIC:
        # Decided by a rule: keep the class, and do not ask for an
        # anchor the rule never owed.
        return (_normalize_failure_class(claimed), SOURCE_DETERMINISTIC)

    if source == SOURCE_DEFAULTED:
        # The record says the class was never earned. It stays
        # unearned, whatever the quote says.
        return (DELIVERABLE_DEFECT, SOURCE_DEFAULTED)

    claimed_class = _claimed_failure_class(claimed)
    if claimed_class is None:
        # A claim outside the vocabulary (or no claim at all) was never
        # earned: there is nothing to keep.
        return (DELIVERABLE_DEFECT, SOURCE_DEFAULTED)

    quote = _verdict_field(verdict, "evidence_quote")
    actual_result = _verdict_field(verdict, "actual_result")
    if not validate_evidence_quote(quote, actual_result):
        return (DELIVERABLE_DEFECT, SOURCE_DEFAULTED)

    return (claimed_class, SOURCE_LLM)


# ---------------------------------------------------------------------------
# The method strength table
# ---------------------------------------------------------------------------
#
# A broken criterion may be replaced — but the replacement may not be a
# *weaker* check than the one it replaces. "Weaker" needs a definition,
# and this table is it: the methods are ranked by how much assurance
# their pass actually carries.
#
# The ranking, strongest first:
#
# ``full_ci``
#     The repository's own gate — lint plus every test, executed by
#     the framework, judged by its exit code. The broadest scope and
#     no model in the loop.
# ``e2e``
#     A real browser running a real user flow. Real observations of
#     the whole flow, though an agent has to report them.
# ``api_test``
#     A real interface, real assertions, framework-executed and
#     therefore objective — but it only sees the interface surface.
# ``code_review``
#     The model reading the source. It sees semantics no runtime
#     check exposes, but the judgement is the model's.
# ``ui_validation``
#     The model reading the checkpoints of a single interaction. The
#     narrowest scope, and the judgement is still the model's.
#
# Two rules decide the order, and they point the same way. Scope first:
# a method that can only observe part of what the criterion is about
# is looser than one that can observe all of it, whatever else it
# costs. Objectivity second: between two methods of the same scope,
# the one the framework runs beats the one a model reads, because a
# model can be talked into a pass and an exit code cannot.
#
# The table is deliberately a *ranking* and not a comparison of two
# unrelated methods: it says which of two checks carries more
# assurance, which is the only question the replacement guard asks.
# It is also deliberately conservative — a method the table does not
# carry is incomparable, and the guard refuses incomparable pairs.
METHOD_STRENGTH: Dict[str, int] = {
    "full_ci": 5,
    "e2e": 4,
    "api_test": 3,
    "code_review": 2,
    "ui_validation": 1,
}


def _normalize_method(method) -> Optional[str]:
    """The canonical spelling of a verification method, or ``None``.

    The same dialect tolerance the signal table has: a caller that
    spells a method differently must not get a different answer for
    the same method. A method the table does not carry returns
    ``None`` rather than a guess — "not in the table" and "weakest
    method in the table" are very different answers, and only one of
    them is safe to build on.
    """
    if method is None:
        return None
    key = re.sub(r"[\s\-]+", "_", str(method).strip().lower()).strip("_")
    if key in METHOD_STRENGTH:
        return key
    return None


def is_not_looser(original_method, replacement_method) -> bool:
    """Whether ``replacement_method`` is at least as strong as the original.

    A broken criterion may be replaced, but the replacement may not be
    a weaker check than the one it replaces: that is how a plan
    silently trades a real check for a rubber stamp. The comparison
    is made against the fixed :data:`METHOD_STRENGTH` table.

    The guard fails closed. A method the table does not carry is
    incomparable, and an incomparable pair is refused — guessing that
    an unknown method is strong enough is precisely the mistake this
    function exists to prevent.

    Args:
        original_method: The method the criterion was verified with.
        replacement_method: The method proposed for the replacement
            criterion.

    Returns:
        ``True`` when the replacement is at least as strong as the
        original (the same method included — it is not looser than
        itself), ``False`` when it is weaker, unknown or missing.
    """
    original = _normalize_method(original_method)
    replacement = _normalize_method(replacement_method)
    if original is None or replacement is None:
        return False
    return METHOD_STRENGTH[replacement] >= METHOD_STRENGTH[original]


__all__ = [
    "FailureClass",
    "Disposition",
    "SOURCE_DETERMINISTIC",
    "SOURCE_LLM",
    "SOURCE_DEFAULTED",
    "ALL_FAILURE_CLASSES",
    "INFRA",
    "DELIVERABLE_DEFECT",
    "CRITERION_DEFECT",
    "OUT_OF_SCOPE",
    "METHOD_STRENGTH",
    "classify_deterministic",
    "classify_verdict",
    "decision_key",
    "disposition_for",
    "is_counted",
    "is_not_looser",
    "validate_evidence_quote",
]