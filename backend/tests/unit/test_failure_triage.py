"""The four-class failure triage base (P0-2, tier A).

P0-2 asks every verification verdict to carry the *nature* of the
failure, not just the fact of it. The four classes:

``infra``
    The environment failed, not the work: a rate limit, a timeout, a
    watchdog kill, an answer that could not be parsed. The existing
    self-heal loop already calls these "attempt malfunctions" (see
    ``verification_subagent.py`` — ``VerdictParseError``,
    ``WatchdogKilledError``, transport error), and it already spends its
    retry budget on exactly those and nothing else.
``deliverable_defect``
    The deliverable under verification is genuinely wrong. The work has
    to be redone. Tier A does not name it: the eight deterministic
    signals the PRD pins belong to the other three classes, so this
    one is what the semantic tier — or the conservative default —
    reaches.
``criterion_defect``
    The verification point itself is wrong: it cannot be expressed (a
    schema rejection), cannot be run as written (the request method is
    not one the runner speaks), or collects nothing at all (the
    ``--collect-only`` run comes back empty). No amount of reworking
    the deliverable will ever satisfy it.
``out_of_scope``
    The check reaches outside what was agreed: the diff touches a path
    the task never declared.

Why the four are not interchangeable
------------------------------------
A failure that counts and a failure that does not send the HITL loop in
opposite directions, so the classification is not a label: it decides
whether a human is interrupted, whether the deliverable is reworked, and
whether the same-cause loop counter moves. The rules are therefore
pinned by behaviour rather than by spelling — this file asserts the
class a deterministic signal resolves to, the disposition each class
carries, and which classes move the counter.

The tier boundary
-----------------
``classify_deterministic`` is the *deterministic* tier: pure rules, no
I/O, no model call. A signal it does not know returns ``None`` rather
than a guess, and the caller hands the case to the semantic tier. That
"return None instead of guessing" is a property of its own, pinned
here, so a future contributor cannot quietly widen the rule table into
a guesser.
"""

from __future__ import annotations

import sys
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
# The deterministic tier — the eight signals tier A knows
# ---------------------------------------------------------------------------
#
# The texts are the producers' own wording, not invented ones, so a
# rename on the producer side shows up here as a broken case instead of
# a silently misclassified failure.
DETERMINISTIC_CASES = [
    # (signal, raw error text, expected class)
    (
        "parse_error",
        "VerdictParseError: Invalid JSON: Expecting value: line 1 column 1",
        "infra",
    ),
    ("rate_limited", "429 Too Many Requests", "infra"),
    ("timeout", "HARD TIMEOUT after 120s", "infra"),
    ("watchdog_kill", "watchdog killed the subprocess after 900s of silence", "infra"),
    (
        "schema_rejected",
        "schema: api_test requires a 'request' object with 'method' and 'url'",
        "criterion_defect",
    ),
    (
        "method_mismatch",
        "request.method must be one of ['GET', 'POST'], got 'TRACE'",
        "criterion_defect",
    ),
    (
        "zero_collected",
        "collected 0 items / no tests ran in 0.02s",
        "criterion_defect",
    ),
    (
        "diff_outside_path",
        "refusing to edit a file this task did not declare: src/app.py",
        "out_of_scope",
    ),
]


def test_deterministic_signals():
    """Every deterministic signal resolves to its class — and to a
    class, not to ``None``.

    ``None`` means "the deterministic tier has no opinion, hand it to
    the semantic tier". A signal that tier A *does* know must never
    come back as ``None``, and it must never come back as a different
    class: the whole point of tier A is that a rate limit is decided by
    a rule, not by a model that may be having a bad day.
    """
    triage = _triage()

    for signal, text, expected in DETERMINISTIC_CASES:
        assert triage.classify_deterministic(signal, text) == expected, (
            f"signal {signal!r} (text {text!r}) should classify as "
            f"{expected!r}"
        )

    # The source of every classification made here is the deterministic
    # tier, by construction: the semantic tier owns the cases tier A
    # returns None for.
    assert triage.SOURCE_DETERMINISTIC == "deterministic"


def test_unknown_signal_none():
    """An unknown signal is deferred, not guessed.

    Returning a class for a signal the rule table does not contain
    would move the decision from a rule to whoever wrote the nearest
    matching rule — which is how a rate limit ends up filed as a
    deliverable defect and the deliverable gets reworked for an
    environment problem.
    """
    triage = _triage()

    assert triage.classify_deterministic("cosmic_rays", "nothing to see here") is None
    assert triage.classify_deterministic("unknown_signal", "") is None
    assert triage.classify_deterministic("", "nothing to see here") is None
    assert triage.classify_deterministic(None, None) is None

    # An unknown signal is deferred even when the text alone would have
    # matched: the caller said which event it is looking at, and the
    # spelling it used is simply not in the table. Guessing from the
    # text here would file the failure under a class the caller never
    # asked about.
    assert triage.classify_deterministic("cosmic_rays", "429 Too Many Requests") is None


def test_signal_spellings_are_normalised():
    """One event, one answer — whatever dialect the caller speaks.

    The producers spell the same event several ways (``"rate_limit"``,
    ``"Rate Limited"``, ``"RATE-LIMITED"``, ``"http_429"``). They are
    one event, so they must be one answer: a caller that spells the
    signal differently must not get a different verdict for the same
    failure, and must not silently fall through to the semantic tier
    for a rate limit the rule table already covers.
    """
    triage = _triage()

    for spelling in (
        "rate_limited",
        "rate_limit",
        "Rate Limited",
        "RATE-LIMITED",
        "http_429",
        "too_many_requests",
    ):
        assert triage.classify_deterministic(spelling, "429 Too Many Requests") == (
            "infra"
        ), f"{spelling!r} should normalise to the rate_limited signal"

    # The same holds for the other two classes' spellings.
    assert triage.classify_deterministic("Schema Rejected", "x") == "criterion_defect"
    assert triage.classify_deterministic("zero collected", "x") == "criterion_defect"
    assert triage.classify_deterministic("diff outside path", "x") == "out_of_scope"


# ---------------------------------------------------------------------------
# The PRD's eight signals — tier A must name every one of them
# ---------------------------------------------------------------------------
#
# The decision point fixes the mapping, and the mapping is what the
# dispositions hang off: a signal filed under the wrong class moves
# the same-cause counter in the wrong direction and disposes of the
# attempt wrongly. Eight signals, three classes, and every one of them
# an *explicit* mapping — a signal that fell through to the default
# branch would be filed as a broken deliverable whatever actually
# happened, and "no tests collected" read as "the product is broken"
# is exactly the confusion the four classes exist to prevent.
PRD_SIGNAL_CLASSES = {
    # infra: the attempt malfunctioned, the work is not implicated
    "parse_error": "infra",
    "rate_limited": "infra",
    "timeout": "infra",
    "watchdog_kill": "infra",
    # criterion_defect: the check cannot be run as written
    "schema_rejected": "criterion_defect",
    "method_mismatch": "criterion_defect",
    "zero_collected": "criterion_defect",
    # out_of_scope: the check reaches outside the declared scope
    "diff_outside_path": "out_of_scope",
}

#: The text markers the callers fall back to when they have the raw
#: error text but no signal name. The wording is the producers' own,
#: so a rename on the producer side shows up here as a broken case
#: instead of a silently misclassified failure.
TEXT_MARKER_CASES = [
    ("collected 0 items", "criterion_defect"),
    ("no tests ran in 0.02s", "criterion_defect"),
    ("no tests collected", "criterion_defect"),
    (
        "[pdt] refusing to edit a file this task did not declare: src/app.py",
        "out_of_scope",
    ),
    ("undeclared_write: src/app.py", "out_of_scope"),
]


def test_the_eight_prd_signals_hit_an_explicit_mapping():
    """Each of the eight signals resolves to the class the PRD names.

    ``None`` would send the case to the semantic tier, and the wrong
    class would dispose of the attempt wrongly — the three classes the
    PRD moves are not interchangeable (one is reworked and counted,
    one is escalated and counted, one is skipped and not counted). The
    text is deliberately empty: the mapping is a property of the
    signal, not of the wording that happens to accompany it.
    """
    triage = _triage()

    assert len(PRD_SIGNAL_CLASSES) == 8, (
        "the decision point names eight signals; the table above has "
        f"{len(PRD_SIGNAL_CLASSES)}"
    )

    for signal, expected in PRD_SIGNAL_CLASSES.items():
        got = triage.classify_deterministic(signal, "")
        assert got == expected, (
            f"signal {signal!r} should classify as {expected!r}, got {got!r}"
        )

    # None of the eight may reach the default branch: the PRD names a
    # class for every one of them, and the default class is the one a
    # signal reaches by *not* being named.
    for signal, expected in PRD_SIGNAL_CLASSES.items():
        assert expected != triage.DELIVERABLE_DEFECT, (
            f"signal {signal!r} is filed under the default class"
        )


def test_the_text_markers_follow_the_same_mapping():
    """The text fallback routes to the same classes as the signals.

    A caller that has the raw error text but no signal name must land
    on the same class as one that has the name — otherwise the same
    failure would be disposed of two different ways depending on how
    much the caller knew.
    """
    triage = _triage()

    for text, expected in TEXT_MARKER_CASES:
        got = triage.classify_deterministic("", text)
        assert got == expected, (
            f"text {text!r} should classify as {expected!r}, got {got!r}"
        )


def test_tier_a_reaches_all_four_classes():
    """The deterministic layer reaches every class, and the four land on
    four different dispositions.

    Three of the four are named by rules: the eight signals above and
    the text markers beside them. The fourth, ``deliverable_defect``,
    is what the *default* branch produces — an unclassifiable failure
    is filed as a broken deliverable, because that is the only class
    whose verdict is safe to act on without a human. That branch is a
    rule too, so the deterministic layer covers all four classes.

    The four dispositions being different is what makes the
    classification a decision rather than a label: a class sharing a
    disposition with another would change nothing.
    """
    triage = _triage()

    reached = {
        triage.classify_deterministic(signal, "")
        for signal in PRD_SIGNAL_CLASSES
    }
    # The default branch, twice: a verdict that names no class at all,
    # and one that names a class outside the vocabulary.
    reached.add(triage.classify_verdict({"verdict": "FAILED"})[0])
    reached.add(
        triage.classify_verdict(
            {
                "verdict": "FAILED",
                "failure_class": "not_a_class",
                "failure_class_source": triage.SOURCE_DETERMINISTIC,
            }
        )[0]
    )

    assert reached == set(triage.ALL_FAILURE_CLASSES), (
        f"tier A reaches {sorted(reached)}, not all four classes"
    )

    dispositions = [
        triage.disposition_for(failure_class)
        for failure_class in triage.ALL_FAILURE_CLASSES
    ]
    assert len(set(dispositions)) == len(triage.ALL_FAILURE_CLASSES), (
        f"two classes share a disposition: {dispositions!r}"
    )


# ---------------------------------------------------------------------------
# The disposition table — what the loop does with each class
# ---------------------------------------------------------------------------
DISPOSITION_TABLE = [
    ("infra", "retry_not_counted"),
    ("deliverable_defect", "retry_counted"),
    ("criterion_defect", "escalate_counted"),
    ("out_of_scope", "skip_not_counted"),
]


def test_disposition_table():
    """Each class carries exactly one disposition, and no two classes
    share one.

    A shared disposition would make the classification decorative: the
    loop would behave the same for a rate limit and for a criterion
    that can never be satisfied, which is the behaviour P0-2 exists to
    change.
    """
    triage = _triage()

    for failure_class, expected in DISPOSITION_TABLE:
        assert triage.disposition_for(failure_class) == expected, (
            f"{failure_class!r} should dispose as {expected!r}, got "
            f"{triage.disposition_for(failure_class)!r}"
        )

    dispositions = [
        triage.disposition_for(failure_class)
        for failure_class, _ in DISPOSITION_TABLE
    ]
    assert len(set(dispositions)) == len(dispositions), (
        f"two classes share a disposition: {dispositions!r}"
    )


def test_is_counted():
    """Only the failures a human should see move the same-cause counter.

    An infrastructure blip is not evidence that the work is wrong, and
    an out-of-scope check is not evidence of anything. Counting either
    would escalate a healthy run to a human and, worse, would make the
    counter say "same cause, three times" about failures that have
    nothing in common.
    """
    triage = _triage()

    assert triage.is_counted("infra") is False
    assert triage.is_counted("out_of_scope") is False
    assert triage.is_counted("deliverable_defect") is True
    assert triage.is_counted("criterion_defect") is True

    # The counter and the disposition must not disagree: a counted
    # failure is exactly one whose disposition says so. The negative
    # form is the one tested for, because "retry_not_counted" also ends
    # in "_counted" — testing for the positive form alone would claim
    # the infrastructure failures count.
    for failure_class, disposition in DISPOSITION_TABLE:
        counted = not disposition.endswith("_not_counted")
        assert triage.is_counted(failure_class) is counted, (
            f"{failure_class!r} counts={triage.is_counted(failure_class)} "
            f"but its disposition is {disposition!r}"
        )


# ---------------------------------------------------------------------------
# The decision key — what makes two failures "the same failure"
# ---------------------------------------------------------------------------
def test_decision_key():
    """The key is the pair (verification point, nature of the failure).

    Two failures of the same class on the same point are one decision;
    the same class on a different point is another. The separator is a
    pipe, so the key stays readable in a log line.
    """
    triage = _triage()

    assert triage.decision_key("VP-007", "criterion_defect") == (
        "VP-007|criterion_defect"
    )
    assert triage.decision_key("VP-007", "infra") == "VP-007|infra"
    assert triage.decision_key("VP-001", "infra") != triage.decision_key(
        "VP-007", "infra"
    )


def test_decision_key_for_a_missing_or_unknown_class_is_a_deliverable_defect():
    """An unstated failure nature is read as a deliverable defect.

    It is the only class whose verdict is safe to act on when nobody
    has said otherwise: the other three either do not count or demand
    a human, and reaching one of those by accident is the expensive
    mistake this default exists to prevent.
    """
    triage = _triage()

    assert triage.decision_key("VP-007", None) == "VP-007|deliverable_defect"
    assert triage.decision_key("VP-007", "") == "VP-007|deliverable_defect"
    assert triage.decision_key("VP-007", "not_a_class") == (
        "VP-007|deliverable_defect"
    )


def test_decision_key_for_a_passed_verdict_is_empty():
    """A verdict that passed has no failure, so it has no decision key.

    An empty key is the shape the loop can count on: a passed verdict
    must never join a same-cause group, or a point that once failed and
    later passed would still be reported as "the same failure, again".
    """
    triage = _triage()

    assert triage.decision_key("VP-007", "deliverable_defect", verdict="PASSED") == ""
    assert triage.decision_key("VP-007", None, "PASSED") == ""


# ---------------------------------------------------------------------------
# The CI gate's three states (2026-10-31, VP-020)
# ---------------------------------------------------------------------------
#
# ``scripts/run_tests.sh`` exits 0 (green), 1 (a lane went red) or 3
# (the run could not decide — the host could not be measured).  The
# framework read every non-zero exit as "the deliverable is broken", so
# a busy host — not a broken delivery — produced a repair task.
#
# The gate's own exit code is the machine-readable fact, so tier A has
# to know it by name: a signal the table does not carry is a ``None``,
# and a ``None`` is handed to the semantic tier, which lands on
# ``deliverable_defect`` — the class that counts and that becomes a
# repair task.
CI_GATE_SIGNAL_CLASSES = [
    # the run could not decide: the machine's problem — retry, do not count
    ("gate_inconclusive", "infra"),
    # the entry could not be started at all: also the machine's problem
    ("gate_not_executable", "infra"),
    # a lane went red: the code under test is implicated — rework, count
    ("gate_failed", "deliverable_defect"),
]


def test_the_ci_gate_signals_hit_an_explicit_mapping():
    """Each gate signal resolves to its class, by rule and not by guess."""
    triage = _triage()

    for signal, expected in CI_GATE_SIGNAL_CLASSES:
        got = triage.classify_deterministic(signal, "")
        assert got == expected, (
            f"signal {signal!r} should classify as {expected!r}, got {got!r}"
        )


def test_a_machine_problem_is_not_counted_against_the_deliverable():
    """The whole point of the three states: the counter moves for the
    code, not for the host.

    A gate that could not decide is worth another try; a gate that went
    red is a defect and has to be reworked.
    """
    triage = _triage()

    inconclusive = triage.classify_deterministic("gate_inconclusive", "")
    failed = triage.classify_deterministic("gate_failed", "")

    assert triage.disposition_for(inconclusive) == "retry_not_counted"
    assert triage.is_counted(inconclusive) is False
    assert triage.disposition_for(failed) == "retry_counted"
    assert triage.is_counted(failed) is True


def test_the_text_fallback_reads_the_gate_wording():
    """A caller that has only the raw text must land on the same class.

    The framework's own timeout reason is Chinese ("超时：3600s 内没有跑
    完") and the gate's own wording is English ("gate: INCONCLUSIVE …
    this is not a code failure"), so both spellings have to be in the
    table — otherwise the same event is disposed of two different ways
    depending on how much the caller knew.
    """
    triage = _triage()

    assert triage.classify_deterministic("", "超时：3600s 内没有跑完") == "infra"
    assert triage.classify_deterministic(
        "", "gate: INCONCLUSIVE (exit 3) — the run could not decide"
    ) == "infra"
    assert triage.classify_deterministic(
        "", "gate: FAIL (exit 1) — a phase failed"
    ) == "deliverable_defect"
    assert triage.decision_key("VP-007", "PASSED") == ""