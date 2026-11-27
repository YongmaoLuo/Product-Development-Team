"""An UNKNOWN entry leaves the round unjudged -- it does not pass it.

Background
----------
``overall_status`` is the one word every reader of a verification report
takes the round's verdict from.  It is derived by one function,
``verification_agent._derive_overall_status``, and the tiers it walks
are::

    a FAILED entry          -> "FAILED"
    a PARTIAL entry         -> "PARTIAL"
    a DEFERRED entry        -> "SKIPPED"
    an environment-blocked  -> "SKIPPED"
    anything else           -> "PASSED"

2026-11-01 (task 1) gave the sub-agent a third verdict for the case it
had been lying about: a verification point whose verdict could not be
parsed -- the sub-agent answered something that was not a verdict at all
-- used to be filed as ``FAILED``, and now carries
``verification_subagent.VERDICT_UNKNOWN``.  The point established
**nothing** about the deliverable: it did not pass, and it did not fail.

Such an entry used to fall into "anything else" and be read as
agreement.  A round holding one UNKNOWN entry and one PASSED entry came
back as ``PASSED``: the point nobody judged was counted as a point that
passed.  That is the same bug the DEFERRED tier already fixed, one tier
down.

What these tests pin
--------------------
  1. an UNKNOWN entry makes the round unjudged -- ``SKIPPED``, not
     ``PASSED``;
  2. the tiers that already outrank "otherwise" keep outranking it: a
     real FAILED still wins over an UNKNOWN, and so does a PARTIAL;
  3. a round whose every entry is UNKNOWN is unjudged too;
  4. the rounds that already said the truth keep saying it: an empty
     round stays PASSED, and an entry that is not a dict or that carries
     no status takes part in no tier at all;
  5. the derivation reads the shared constant
     ``verification_subagent.VERDICT_UNKNOWN`` rather than a second
     spelling of it.

The interface contract
-----------------------
::

    verification_agent._derive_overall_status(entries: list[dict]) -> str

``entries`` are the report's ``verification_results``.  The decision is
taken in this order::

    a FAILED entry          -> "FAILED"
    a PARTIAL entry         -> "PARTIAL"
    an UNKNOWN entry        -> "SKIPPED"
    a DEFERRED entry        -> "SKIPPED"
    an environment-blocked  -> "SKIPPED"
    anything else           -> "PASSED"
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_agent  # noqa: E402
import verification_subagent  # noqa: E402


def _derive(entries):
    """The round's verdict, derived from the round's entries."""
    derive = getattr(verification_agent, "_derive_overall_status", None)
    assert derive is not None, (
        "verification_agent has no module-level _derive_overall_status: "
        "the round's verdict is still derived by an ad-hoc branch that "
        "reads 'otherwise' as PASSED"
    )
    return derive(entries)


# ---------------------------------------------------------------------------
# The reproduction
# ---------------------------------------------------------------------------


def test_unknown_entry_makes_round_unjudged():
    """An UNKNOWN entry makes the round unjudged, not passed.

    One point came back UNKNOWN -- the sub-agent's answer was not a
    verdict, so the point established nothing about the deliverable --
    and one point passed.  The round judged half of itself; it did not
    pass.
    """
    entries = [
        {"vp_id": "VP-017", "status": "UNKNOWN"},
        {"vp_id": "VP-018", "status": "PASSED"},
    ]

    assert _derive(entries) == "SKIPPED", entries


# ---------------------------------------------------------------------------
# The tiers that outrank "otherwise"
# ---------------------------------------------------------------------------


def test_failed_still_wins_over_unknown():
    """A real failure outranks an unjudged point."""
    entries = [
        {"vp_id": "VP-017", "status": "FAILED"},
        {"vp_id": "VP-018", "status": "UNKNOWN"},
    ]

    assert _derive(entries) == "FAILED", entries


def test_partial_still_wins_over_unknown():
    """A partial verdict is not downgraded to a skip by an UNKNOWN."""
    entries = [
        {"vp_id": "VP-017", "status": "PARTIAL"},
        {"vp_id": "VP-018", "status": "UNKNOWN"},
    ]

    assert _derive(entries) == "PARTIAL", entries


def test_all_unknown_is_skipped():
    """A round that judged nothing at all is unjudged.

    Every entry is UNKNOWN: no point passed and no point failed, and a
    round that judged nothing has no verdict to give.
    """
    entries = [
        {"vp_id": "VP-017", "status": "UNKNOWN"},
        {"vp_id": "VP-018", "status": "UNKNOWN"},
    ]

    assert _derive(entries) == "SKIPPED", entries


# ---------------------------------------------------------------------------
# The rounds that already said the truth
# ---------------------------------------------------------------------------


def test_empty_entries_still_passed():
    """A round with no verification point keeps its verdict.

    There is nothing to judge, so there is nothing to complain about
    either -- and that is the behaviour the zero-verification-point
    rounds have always had.  This case is out of scope for the change
    and must not move.
    """
    assert _derive([]) == "PASSED"


def test_entries_without_a_status_take_part_in_no_tier():
    """An entry the round could not read takes part in no tier.

    A non-dict entry and an entry that carries no status are not
    evidence of anything -- neither of a failure, nor of a partial
    verdict, nor of an unjudged point.  If they took part in the
    UNKNOWN tier, a round of nothing but unreadable entries would come
    back unjudged instead of keeping the verdict it always had.
    """
    entries = [
        "not a dict",
        {"vp_id": "VP-017"},
        None,
    ]

    assert _derive(entries) == "PASSED", entries


def test_all_passed_stays_passed():
    """Every point passed: the round passes."""
    entries = [{"status": "PASSED"}] * 3

    assert _derive(entries) == "PASSED", entries


# ---------------------------------------------------------------------------
# The shared constant
# ---------------------------------------------------------------------------


def test_the_agent_reads_the_shared_unknown_verdict_constant():
    """One spelling of the verdict, shared with the sub-agent.

    The executor already accepts ``UNKNOWN`` as a status and the
    sub-agent already writes it; the derivation reads the same constant
    rather than a second literal that could drift away from it.
    """
    assert verification_subagent.VERDICT_UNKNOWN == "UNKNOWN"

    agent_constant = getattr(verification_agent, "VERDICT_UNKNOWN", None)
    assert agent_constant == verification_subagent.VERDICT_UNKNOWN, (
        "verification_agent does not read verification_subagent's "
        "VERDICT_UNKNOWN; the derivation would be spelling the verdict "
        "a second time"
    )
