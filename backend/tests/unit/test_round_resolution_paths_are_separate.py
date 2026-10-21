"""The two round-resolution paths must stay separate, not merely coexist.

``_plan_next_round`` and ``_resolve_start_round`` answer the same
question -- "which round number is next?" -- from the same kind of input
(a ``plan_verification`` row), and they answer it **differently on
purpose**:

  * ``verification_loop._plan_next_round`` runs after a round genuinely
    *finished*. Whatever the row looks like, the finished round is
    behind us, so the answer is unconditionally ``round + 1`` against
    the plan's immutable cap.

  * ``routes.verification._resolve_start_round`` runs at ``POST /start``
    and has to cope with a round that was *cut short*. An interrupted
    round must be resumed in place -- the round number is the contract a
    plan's verdicts, report and repair tasks are written against -- so
    the answer is ``(round, resumed=True)`` for a row whose status is
    still ``running`` / ``loop_stopped``.

Mixing them is not a style question. If the auto-loop path were handed
the resume rule, an interrupted round would be counted as a completed
one and the plan would skip its number; if ``/start`` were handed the
``+1`` rule, a half-finished round would be silently relabelled as the
next one and its work lost. Only comments used to say so.

Both are pure functions of their arguments -- no thread, no DB -- so
they are pinned here directly. The endpoint-level view of the same
behaviour (``POST /api/verification/{plan_id}/start`` with a paused
checkpoint) is layered separately; this file is the unit beneath it.
"""

from __future__ import annotations

import pytest

import server as server_mod
from verification_loop import _plan_next_round
from routes.verification import _resolve_start_round


# ---------------------------------------------------------------------------
# auto-loop path: a finished round always advances, interruption traces and all
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status",
    ["running", "loop_stopped"],
)
def test_plan_next_round_ignores_interruption_traces(status):
    """The auto-loop runs one round *after* one completed, so it advances.

    ``running`` / ``loop_stopped`` on the row are traces of an
    interruption, but they are not consulted here: the caller only gets
    here once the round is done. A row that still looks interrupted must
    still yield ``round + 1`` -- if this ever returns the same round, the
    loop has started skipping numbers.
    """
    assert _plan_next_round(
        {"round": 2, "max_rounds": 3, "verification_status": status},
    ) == (3, 3, False)


def test_plan_next_round_without_row_starts_at_one():
    """No row at all: the plan starts at round 1 against the default cap."""
    assert _plan_next_round(None) == (
        1,
        server_mod.DEFAULT_MAX_VERIFICATION_ROUNDS,
        False,
    )


def test_plan_next_round_exhausts_at_the_immutable_cap():
    """The cap is the plan's own budget and nothing raises it."""
    assert _plan_next_round({"round": 3, "max_rounds": 3}) == (4, 3, True)


def test_plan_next_round_and_resolve_start_round_disagree_on_one_row():
    """The separation itself, asserted as a fact about the same input.

    This is the property the whole file exists for. If the two paths ever
    converge -- either one consulting the other's interruption traces --
    the pairs below become equal and this fails.
    """
    row = {"round": 2, "max_rounds": 3, "verification_status": "loop_stopped"}

    planned = _plan_next_round(dict(row))
    resolved, resumed = _resolve_start_round(dict(row), None)

    assert planned == (3, 3, False)          # auto-loop: round 2 is done
    assert (resolved, resumed) == (2, True)  # /start: round 2 is unfinished
    assert planned[0] != resolved


# ---------------------------------------------------------------------------
# /start path: an interrupted round is resumed in place, never renumbered
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status",
    ["running", "loop_stopped"],
)
def test_resolve_start_round_resumes_from_row_status(status):
    """A row left in an in-flight state is the round that gets resumed.

    The process died before it could write a checkpoint, so the row is
    all that is left of the interruption: ``round > 0`` plus a status
    that never reached a terminal value is enough to say "this round is
    unfinished", and it must be picked back up at its own number.
    """
    assert _resolve_start_round(
        {"round": 2, "verification_status": status}, None,
    ) == (2, True)


def test_resolve_start_round_advances_after_a_passed_round():
    """A finished round really did finish: the next round is the next one.

    This is the contrast case. Without the status check every row would
    look interrupted, so ``resumed`` would never be ``False``.
    """
    assert _resolve_start_round(
        {"round": 2, "verification_status": "passed"}, None,
    ) == (3, False)


def test_resolve_start_round_without_a_row_has_nothing_to_resume():
    """No row and no checkpoint: an ordinary fresh start at round 1."""
    assert _resolve_start_round(None, None) == (1, False)