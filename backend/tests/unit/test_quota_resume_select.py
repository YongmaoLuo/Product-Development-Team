"""Which parked objects does a recovered quota window revive?

The criterion this file pins
----------------------------
Requirement R3 of the quota-park work says, in one line: *only revive
what was parked because of quota*.  That sentence is the whole
difficulty, because "parked because of quota" and "not running" are not
the same thing, and a reader that conflates them revives the wrong
plans:

* a plan a human ``/stop``-ed is not running — but reviving it undoes
  the human;
* a plan that failed is not running — but reviving it re-runs work
  whose verdict was already written;
* a plan parked because every provider ran out of room is not running —
  and reviving it is exactly the point.

So the criterion has to be a *conjunction*, and both halves have to be
read from the durable row rather than inferred:

* the row carries the quota marker (``waiting_for_quota``) — this is the
  "because of quota" half, and it is what says the run stopped for
  capacity rather than for any other reason;
* the row's status is not a terminal one (``failed`` / ``loop_stopped`` /
  ``stopped``) — this is the "may be revived" half, and it is what keeps
  a plan that was parked and *then* failed from being brought back.

The other half of the criterion is the *reading* of the quota window
itself.  A probe that could not read the payload returns an empty
mapping, and an empty mapping is not evidence of an open window: it is
evidence of nothing at all.  Treating "I could not see a closed window"
as "the window is open" would wake the whole fleet on a network blip —
which is worse than sleeping, because every woken plan then finds the
window still shut and parks again.

What these tests are for
------------------------
They pin the *decision*, not the plumbing: no database, no server, no
sleep.  Four readers will share this criterion — the table reader, the
resumer, the heartbeat and the scheduler's read model — and the only
way four readers can be kept from drifting is for them to share one
pure function and for that function's edges to be nailed down here.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from quota_resume import (  # noqa: E402
    ParkedPlan,
    quota_recovered,
    select_parked_plans,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _provider(exhausted: bool) -> SimpleNamespace:
    """One provider's quota reading, shaped like the reader's entry."""
    return SimpleNamespace(exhausted=exhausted)


def _execution_row(plan_id: str, **overrides: object) -> dict:
    """A ``plan_execution`` row, as the repository hands it over."""
    row = {
        "plan_id": plan_id,
        "exec_status": "waiting_for_quota",
        "stop_reason": "waiting_for_quota",
    }
    row.update(overrides)
    return row


def _verification_row(plan_id: str, **overrides: object) -> dict:
    """A ``plan_verification`` row, as the repository hands it over."""
    row = {
        "plan_id": plan_id,
        "verification_status": "waiting_for_quota",
        "verification_stop_reason": "waiting_for_quota",
        "round": 1,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# The quota reading
# ---------------------------------------------------------------------------


def test_empty_reading_is_not_recovery():
    """A reading that came back empty is not an open window.

    The watchdog reports a failed read as an empty mapping, and the
    reader inside it keeps every provider selectable on that answer —
    which is right for *dispatch* (a provider that cannot be seen is a
    provider that may be tried) and wrong for *resumption* (a window
    nobody could see is not a window that opened).  Waking the fleet on
    a read that failed would turn a network blip into a thundering herd
    of plans that immediately park again.
    """
    assert quota_recovered({}) is False


def test_one_available_provider_is_recovery():
    """One provider with room left is enough to call the window open.

    The window is a property of the fleet, and one open provider is
    enough to serve it: the fleet is throttled per provider, so a single
    provider with capacity can carry the work that was parked.
    """
    quota = {"p1": _provider(False), "p2": _provider(True)}

    assert quota_recovered(quota) is True


def test_all_exhausted_is_not_recovery():
    """Every provider shut means the window is still closed."""
    quota = {"p1": _provider(True), "p2": _provider(True)}

    assert quota_recovered(quota) is False


# ---------------------------------------------------------------------------
# The selection
# ---------------------------------------------------------------------------


def test_execution_wins_over_verification():
    """A plan parked in both tables is revived once, as an execution.

    A parked plan can be parked in both places at the same time — the
    execution parked, and the verification round that was going to judge
    it parked behind the same closed window.  Reviving it twice would
    break the single-instance rule the whole plan rests on (one
    execution, one verification), and the second one would collide with
    the first over the same rows.

    Execution goes first because it is the earlier stage: the execution
    still has work to do, and the verification round is waiting for
    that work.  Reviving the execution is the only half that can make
    progress; reviving the verification first would have it judge a plan
    that has not run.
    """
    execution_rows = [_execution_row("p9")]
    verification_rows = [_verification_row("p9", round=2)]

    assert select_parked_plans(execution_rows, verification_rows) == [
        ParkedPlan(
            plan_id="p9",
            kind="execution",
            round=0,
            reason="waiting_for_quota",
        )
    ]


def test_terminal_rows_are_not_selected():
    """A terminal row is never revived, whatever its reason says.

    Requirement R3 names these explicitly: a plan that failed or was
    stopped has reached an end, and the verdict that ended it stands.
    The marker survives on the row — that is what makes the row
    interesting to a reader — but "parked once" does not mean "parked
    now", and reviving a failed plan would re-run work whose outcome
    was already written down.
    """
    execution_rows = [
        _execution_row("p1", exec_status="failed"),
        _execution_row("p2", exec_status="stopped"),
    ]
    verification_rows = [
        _verification_row("p3", verification_status="loop_stopped"),
    ]

    assert select_parked_plans(execution_rows, verification_rows) == []


# ---------------------------------------------------------------------------
# The edges around the criterion
# ---------------------------------------------------------------------------


def test_verification_park_keeps_its_round():
    """A verification park is revived on the round it stopped on.

    The round is not a detail: the next round is computed from it, and
    re-running a *new* round would restart the judgement instead of
    continuing it — throwing away the verdicts the parked round had
    already reached.
    """
    verification_rows = [_verification_row("p4", round=3)]

    assert select_parked_plans([], verification_rows) == [
        ParkedPlan(
            plan_id="p4",
            kind="verification",
            round=3,
            reason="waiting_for_quota",
        )
    ]


def test_rows_without_the_quota_marker_are_not_selected():
    """A row that stopped for any other reason is not a quota park.

    The marker is the whole of the "because of quota" half.  A row
    that says it stopped — for a human, for a crash, for a timeout —
    has work that finished, and reviving it would be reviving something
    that was not parked at all.
    """
    execution_rows = [
        _execution_row("p5", stop_reason="user_stopped", exec_status="stopped"),
        _execution_row("p6", stop_reason="executor_killed", exec_status="failed"),
    ]
    verification_rows = [
        _verification_row(
            "p7",
            verification_stop_reason="verification_thread_died_unexpectedly",
            verification_status="failed",
        ),
    ]

    assert select_parked_plans(execution_rows, verification_rows) == []


def test_rows_may_be_keyed_or_object_rows():
    """A row may arrive as a mapping, as a keyed row, or as an object.

    The durable tables are read through SQLite, and a SQLite row is
    neither a mapping nor an attribute-addressable object: it answers
    ``row["column"]`` and nothing else.  A reader that understood only
    dictionaries would read every column of such a row as missing, and
    a park would go unnoticed — which is the worst way for this
    criterion to fail, because it fails silently.

    So the reader takes a row in whichever of the three shapes the
    caller happens to have.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE plan_execution "
        "(plan_id TEXT, exec_status TEXT, stop_reason TEXT)"
    )
    conn.execute(
        "INSERT INTO plan_execution VALUES (?, ?, ?)",
        ("p10", "waiting_for_quota", "waiting_for_quota"),
    )
    keyed_rows = conn.execute("SELECT * FROM plan_execution").fetchall()

    object_rows = [
        SimpleNamespace(
            plan_id="p11",
            exec_status="waiting_for_quota",
            stop_reason="waiting_for_quota",
        )
    ]

    assert select_parked_plans(keyed_rows, []) == [
        ParkedPlan(plan_id="p10", kind="execution", round=0,
                   reason="waiting_for_quota")
    ]
    assert select_parked_plans(object_rows, []) == [
        ParkedPlan(plan_id="p11", kind="execution", round=0,
                   reason="waiting_for_quota")
    ]


def test_missing_fields_degrade_instead_of_raising():
    """A row with missing fields is answered, not fatal.

    The durable rows are written by several writers, some of them
    best-effort by design, so a row can be missing its status, its
    reason or its plan id.  A reader that raises on such a row turns a
    partial write into a dead resumer — and the partial write is
    exactly the case the resumer exists to handle.  Missing fields
    degrade to the empty value and the row is judged on what it does
    say.
    """
    execution_rows = [{}, {"plan_id": "p8"}]
    verification_rows = [{}]

    assert select_parked_plans(execution_rows, verification_rows) == []


def test_selection_is_sorted_by_plan_id():
    """The result is ordered by plan id.

    The resumer reads this list to decide what to revive, and the order
    it revives things in is the order it takes the single-instance
    locks in.  A stable, plan-id order keeps that deterministic across
    runs and across readers, instead of leaving it to whatever order
    the database happened to hand the rows over in.
    """
    execution_rows = [
        _execution_row("p2"),
        _execution_row("p1"),
    ]
    verification_rows = [_verification_row("p3")]

    assert [entry.plan_id for entry in select_parked_plans(
        execution_rows, verification_rows
    )] == ["p1", "p2", "p3"]
