"""A handed-back round starts FRESH — it is not resumed.

2026-11-01 (verification convergence, §2 / §3): a round that was
BLOCKED before it dispatched any verification point — the base-drift
preflight, the service preflight — does not consume a round number.
``VerificationRepository.release_round`` (task 6) hands the number back:
the counter is rewound by exactly one, and the release is recorded in
``executor_state["released_rounds"]`` as ``{round, reason, released_at}``.

The number was handed back; the row's ``verification_status`` was not
(``release_round`` deliberately leaves it alone). So the row still says
``running`` / ``loop_stopped`` — and that is the collision this file
pins.

``_resolve_start_round`` reads such a row as "an interrupted round" and
resumes it in place. After a release that reading is wrong twice over:

  * the counter was rewound, so the row's ``round`` no longer names the
    round that was attempted — it names the round *below* it;
  * the round that was attempted was **thrown away** — resuming it
    would re-use the state of the attempt that was given back.

So the handed-back number has to start a fresh round, and the number
itself must not be lost: a plan that keeps resuming the round below it
never runs the number it was given back, and stays on the same round
forever.

What is pinned here:

  * a handed-back round starts fresh — the released number is the round
    that runs, and it runs as a NEW round, not as a resume;
  * a genuinely interrupted round is still resumed in place — the
    released-round reading must not swallow ordinary interruptions;
  * a row with no release record behaves exactly as it did before;
  * a checkpoint that names a handed-back round is ignored (it describes
    the attempt that was thrown away); a checkpoint that names a round
    that was NOT handed back still resumes that round;
  * an absent / malformed ``released_rounds`` record is not a reason to
    raise — the old behaviour stands.
"""

from __future__ import annotations

import pytest

# ``server`` seeds ``sys.modules['server']`` and pulls the routers in at
# its own import time; reaching ``routes.verification`` first trips a
# circular import. Same ordering the application itself uses.
import server  # noqa: F401  (imported for its side effect, not used)
from routes.verification import _resolve_start_round


def _row(round_n: int, status: str, released: list) -> dict:
    """A ``plan_verification`` row as the repository hands it back."""
    return {
        "round": round_n,
        "verification_status": status,
        "executor_state": {"released_rounds": released},
    }


# ---------------------------------------------------------------------------
# The handed-back round starts fresh
# ---------------------------------------------------------------------------


def test_released_round_starts_fresh():
    """The number that was handed back is the number that runs — fresh.

    The release rewound the counter from 3 to 2 and left the status at
    ``loop_stopped``. Reading that row as "round 2 was interrupted"
    resumes round 2 and the number 3 is never used — the plan sits on
    the same round forever.

    The handed-back number is the round that runs, and it runs as a
    NEW round: the attempt it replaces was given back, so there is
    nothing to resume.
    """
    row = {
        "round": 2,
        "verification_status": "loop_stopped",
        "executor_state": {
            "released_rounds": [{"round": 3, "reason": "base_drift_conflicts"}],
        },
    }

    assert _resolve_start_round(row, None) == (3, False)


def test_released_round_starts_fresh_after_a_running_row():
    """``running`` reads the same way as ``loop_stopped`` — same answer."""
    row = {
        "round": 2,
        "verification_status": "running",
        "executor_state": {
            "released_rounds": [{"round": 3, "reason": "service_preflight_failed"}],
        },
    }

    assert _resolve_start_round(row, None) == (3, False)


# ---------------------------------------------------------------------------
# A genuine interruption is still resumed
# ---------------------------------------------------------------------------


def test_genuinely_interrupted_round_still_resumes():
    """An ordinary interruption keeps being resumed in place.

    The release record here is HISTORY: round 1 was handed back, the
    plan moved on and round 2 was then genuinely interrupted. The
    released-round reading must not swallow that — round 2 is
    unfinished work and has to be picked back up at its own number.
    """
    row = {
        "round": 2,
        "verification_status": "loop_stopped",
        "executor_state": {
            "released_rounds": [{"round": 1, "reason": "service_preflight_failed"}],
        },
    }

    assert _resolve_start_round(row, None) == (2, True)


def test_released_round_re_run_and_interrupted_still_resumes():
    """A handed-back round that was RE-RUN is not history any more.

    Round 3 was handed back, the plan ran round 3 again and that
    second attempt was interrupted. The counter is back at 3, so the
    release sits below it — there is no pending release left, and the
    interrupted round is resumed like any other.
    """
    row = {
        "round": 3,
        "verification_status": "loop_stopped",
        "executor_state": {
            "released_rounds": [{"round": 3, "reason": "base_drift_conflicts"}],
        },
    }

    assert _resolve_start_round(row, None) == (3, True)


# ---------------------------------------------------------------------------
# No release record → the old behaviour, untouched
# ---------------------------------------------------------------------------


def test_no_released_rounds_keeps_behaviour():
    """A row that never released anything behaves exactly as before."""
    assert _resolve_start_round(
        {"round": 2, "verification_status": "loop_stopped"}, None,
    ) == (2, True)
    assert _resolve_start_round(
        {"round": 2, "verification_status": "running"}, None,
    ) == (2, True)
    assert _resolve_start_round(
        {"round": 2, "verification_status": "passed"}, None,
    ) == (3, False)
    assert _resolve_start_round(None, None) == (1, False)


# ---------------------------------------------------------------------------
# The checkpoint and the release record
# ---------------------------------------------------------------------------


def test_checkpoint_naming_a_released_round_is_ignored():
    """The checkpoint describes the attempt that was thrown away.

    The stop path wrote a checkpoint for round 3; round 3 was then
    handed back. The checkpoint still names round 3 — but round 3 was
    given back, so it starts fresh instead of being resumed.
    """
    row = {
        "round": 2,
        "verification_status": "loop_stopped",
        "executor_state": {
            "released_rounds": [{"round": 3, "reason": "base_drift_conflicts"}],
        },
    }
    checkpoint = {"kind": "verification", "verification_round": 3}

    assert _resolve_start_round(row, checkpoint) == (3, False)


def test_checkpoint_naming_a_genuine_round_still_resumes():
    """A checkpoint that names a round which was NOT handed back wins.

    The checkpoint says round 2 was interrupted; round 2 is not in the
    release record. That is a real interruption and it is resumed.
    """
    row = {
        "round": 2,
        "verification_status": "loop_stopped",
        "executor_state": {
            "released_rounds": [{"round": 3, "reason": "base_drift_conflicts"}],
        },
    }
    checkpoint = {"kind": "verification", "verification_round": 2}

    assert _resolve_start_round(row, checkpoint) == (2, True)


# ---------------------------------------------------------------------------
# A malformed release record is not a reason to raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "executor_state",
    [
        {},
        {"released_rounds": None},
        {"released_rounds": "not-a-list"},
        {"released_rounds": ["not-a-dict"]},
        {"released_rounds": [{"reason": "no round number"}]},
        {"released_rounds": [{"round": "not-a-number"}]},
        {"released_rounds": [{"round": 0}]},
        [],
        "not-an-object",
        None,
    ],
)
def test_malformed_release_record_keeps_behaviour(executor_state):
    """Anything unrecognisable reads as "nothing was handed back".

    The blob is free-form — it can hold a list, a string, entries
    without a round number. None of that may raise into a start: the
    old behaviour stands.
    """
    row = {
        "round": 2,
        "verification_status": "loop_stopped",
        "executor_state": executor_state,
    }

    assert _resolve_start_round(row, None) == (2, True)


def test_release_record_read_from_a_json_string():
    """The blob may still be a JSON string — it is read, not assumed."""
    row = {
        "round": 2,
        "verification_status": "loop_stopped",
        "executor_state": '{"released_rounds": [{"round": 3}]}',
    }

    assert _resolve_start_round(row, None) == (3, False)
