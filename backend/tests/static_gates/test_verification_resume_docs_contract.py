"""The stop / resume contract must be written down where the code lives.

Why this gate exists
--------------------
Stopping a verification round used to be final: the endpoint parked the
routing row and the round was gone. It is now a **pause** — the stop
reads the round's verdicts off the ``plan_verification`` row, snapshots
the interrupted round, writes a pause checkpoint (the round, its
verdicts, the VPs it cut short), and only then kills the in-flight
sub-agents. A later ``/start`` reads that checkpoint and **resumes the
same round**.

That contract lives entirely in the code. Nothing about it is visible
from the outside except the shape of the response body, and the two
functions that implement it describe themselves in ways that are now
wrong:

* ``stop_verification`` said it "CAS[ed] the routing row to idle" —
  a description of the *old* endpoint, one that described the first
  step of five and implied the round was discarded rather than paused.
  A reader of that docstring would not know a checkpoint was written,
  that the verdicts survive, or that a later ``/start`` resumes this
  round instead of starting a new one.
* ``_plan_next_round`` said nothing about the fact that it is **not**
  the resume decision. It answers "which round follows a round that
  really finished", and it always returns ``current.round + 1``. The
  resume decision lives in a different function —
  ``_resolve_start_round`` — which returns the interrupted round
  itself. Two functions, two different questions, one shared word
  ("next round"), and the docstrings did not say so. The failure this
  leaves room for is a resume that advances the round number and
  mislabels work that was already half done.

A wrong docstring is not a red build. It is a comment that actively
contradicts the code, which is worse than no comment at all — it is
read once, believed, and then the reader reasons about the wrong
contract from then on.

What it pins
------------
1. ``stop_verification``'s docstring names the pause, the checkpoint,
   the verdicts, the interrupted VPs, the two helpers that implement
   the pause (``_write_verification_checkpoint``) and the resume
   (``_resolve_start_round``) — and no longer claims the endpoint is
   only a routing-row CAS.
2. ``_plan_next_round``'s docstring names its own scope: it is the
   "a round really finished" question, not the resume decision, and
   ``_resolve_start_round`` is the other half. The existing statement
   that ``max_rounds`` may not be raised must survive verbatim — it is
   the reason the function exists as a separate, testable decision.

The gate reads the docstrings through ``inspect.getdoc`` rather than
by grepping the source, so what is checked is the docstring the
interpreter actually hands a caller — the same text ``help()`` and
Sphinx would show.
"""

from __future__ import annotations

import inspect
import re

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def contract_targets():
    """Return ``(stop_verification, _plan_next_round)`` under test.

    ``server`` has to be imported first: it pulls in the routers at its
    own module scope, and reaching ``routes.verification`` directly
    trips a circular import (``ArchivedPlanError`` is still being
    defined when the router re-enters). Same ordering the application
    itself uses, and the same ordering
    ``backend/tests/unit/notifications/test_verification_round_activity.py``
    relies on.
    """
    import server  # noqa: F401  (imported for its side effect, not used)
    import routes.verification as routes_verification
    import verification_loop

    return (
        routes_verification.stop_verification,
        verification_loop._plan_next_round,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stop_docstring_describes_pause_contract(contract_targets) -> None:
    """``stop_verification`` must document the pause, not the old CAS.

    The endpoint's observable behaviour is a pause: the response says
    ``paused``, and the round it interrupted is on disk with its
    verdicts, so the next ``/start`` resumes it. A docstring that
    describes only the routing-row CAS describes neither — and it is
    actively misleading, because "CAS the row to idle" reads as "the
    round is over and thrown away", which is the opposite of what the
    endpoint now does.
    """
    stop_verification, _ = contract_targets
    doc = inspect.getdoc(stop_verification) or ""
    lowered = doc.lower()

    for token in (
        "pause",
        "checkpoint",
        "verdict",
        "interrupted",
        "_write_verification_checkpoint",
        "_resolve_start_round",
    ):
        assert token in lowered, (
            f"stop_verification's docstring does not mention {token!r}. "
            "The endpoint pauses a round rather than discarding it: it "
            "writes the verdicts and the interrupted VPs into a pause "
            "checkpoint, and a later /start resumes that same round "
            "through _resolve_start_round. A docstring that leaves "
            f"these out reads as the old behaviour. Current text:\n{doc}"
        )

    assert "casing the routing row to idle" not in lowered, (
        "stop_verification's docstring still describes the endpoint as "
        "a routing-row CAS to idle. That was the old contract — the "
        "endpoint now leaves a resumable pause behind, and the CAS is "
        "only its first step. Current text:\n" + doc
    )


def test_plan_next_round_docstring_names_its_scope(contract_targets) -> None:
    """``_plan_next_round`` must say it is not the resume decision.

    Two functions answer "which round is this?" and they disagree by
    design: ``_plan_next_round`` advances past a round that really
    finished, ``_resolve_start_round`` returns the interrupted round
    itself so a resume does not relabel work that was already half
    done. A caller who reaches for the wrong one silently mislabels
    the round, so the docstring has to name the other half.
    """
    _, plan_next_round = contract_targets
    doc = inspect.getdoc(plan_next_round) or ""

    assert "_resolve_start_round" in doc, (
        "_plan_next_round's docstring does not mention "
        "_resolve_start_round. The two functions answer different "
        "questions — this one advances past a finished round, "
        "_resolve_start_round resumes an interrupted one — and a "
        "reader cannot tell them apart without being told. Current "
        f"text:\n{doc}"
    )

    assert "finished" in doc.lower(), (
        "_plan_next_round's docstring does not say which kind of round "
        "it advances past. It is the 'the round really finished' "
        "question; the interrupted case belongs to "
        f"_resolve_start_round. Current text:\n{doc}"
    )

    # The statement that the cap is immutable is the reason this
    # function is a separate, testable decision — it must survive the
    # scope paragraph, not be replaced by it.
    assert re.search(
        r"max_rounds.*?nothing may raise it", doc, re.DOTALL
    ), (
        "_plan_next_round's docstring no longer says that max_rounds "
        "may not be raised. That invariant is what the function exists "
        f"to enforce. Current text:\n{doc}"
    )
