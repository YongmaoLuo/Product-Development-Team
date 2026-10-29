"""A restart can tell "the server went down" from "the process crashed".

Background
----------
The shutdown path leaves a checkpoint behind for every plan that was
still running (``backend/shutdown_checkpoint.py``).  The recovery path
reads that checkpoint back and has to answer one question before it
decides what to do with a plan whose row still says ``running`` while
its pid is gone: *was this interrupted by the shutdown, or did it die?*

The answer is ``is_shutdown_interrupted``, and it takes three things to
say yes:

* **the reason** -- the checkpoint's ``reason`` has to be
  ``server_shutdown``.  A ``user_requested`` stop is a different event
  (somebody pressed stop) and a ``hitl_strict_timeout`` is a different
  event again; neither may be laundered into "the server went down";
* **the kind** -- a verification checkpoint must not make an execution
  look like a shutdown, and the other way round;
* **the freshness** -- the checkpoint's ``paused_at`` must not be
  earlier than the run's ``started_at``.  A checkpoint left over from
  an *earlier* run of the same plan says nothing about this run, and
  believing it would hide a crash behind a shutdown that did not
  happen.

Every failure of those three means the same answer: ``False``, i.e.
"treat this as a crash".  The predicate is deliberately one-sided --
it may only say yes to a shutdown it can actually prove.

The clock
---------
The two stamps do not come from the same clock.  ``pause_state``
writes ``paused_at`` as a timezone-aware UTC stamp, while
``routes.execution`` writes ``started_at`` as a naive local one.  The
predicate has to put both on the same clock before comparing them, or
every judgement is off by the machine's UTC offset.

What these tests pin
--------------------
* a fresh shutdown checkpoint is recognised;
* a stale checkpoint -- older than the run -- is not;
* a ``user_requested`` checkpoint is not a shutdown;
* a verification checkpoint is not an execution;
* a naive ``started_at`` is read as local time;
* an unparseable stamp is not recognised;
* no checkpoint at all is not recognised.

The stamps in these tests are *computed* rather than hard-coded
literals: a naive stamp is derived from an aware one with
``.astimezone()``, so each test means the same instant no matter what
timezone the machine running it is in.
"""

import json
from datetime import datetime, timedelta, timezone

from pause_state import (
    PAUSE_CHECKPOINT_FILENAME,
    write_pause_checkpoint,
)
from shutdown_checkpoint import SHUTDOWN_STOP_REASON, is_shutdown_interrupted


def _write_checkpoint(plan_dir, *, kind, reason, paused_at):
    """Write a checkpoint whose ``paused_at`` the test controls.

    ``write_pause_checkpoint`` stamps ``paused_at`` itself, and the
    freshness judgement is exactly about that field -- so the helper
    goes through the public writer (which owns the schema version) and
    then rewrites the one field the test needs to pin.
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    document = dict(
        write_pause_checkpoint(plan_dir, kind=kind, reason=reason)
    )
    document["paused_at"] = paused_at
    (plan_dir / PAUSE_CHECKPOINT_FILENAME).write_text(
        json.dumps(document), encoding="utf-8"
    )
    return document


def _naive_local(moment):
    """``moment`` (aware) spelled the way the execution state spells it.

    ``routes.execution`` writes ``started_at`` as
    ``datetime.now().isoformat()`` -- a naive *local* timestamp.  The
    helper makes the same conversion, so a test can hand the predicate
    a naive stamp that denotes a known instant.
    """
    return moment.astimezone().replace(tzinfo=None).isoformat()


def test_a_fresh_shutdown_checkpoint_is_recognised(tmp_path):
    """A checkpoint written during this run is recognised as a shutdown."""
    plan_dir = tmp_path / "p1"
    paused = datetime.now(timezone.utc)
    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason=SHUTDOWN_STOP_REASON,
        paused_at=paused.isoformat(),
    )

    # The run started a minute before the pause.
    run_started_at = _naive_local(paused - timedelta(minutes=1))

    assert (
        is_shutdown_interrupted(
            plan_dir, kind="execution", run_started_at=run_started_at
        )
        is True
    )


def test_a_stale_checkpoint_is_not_recognised(tmp_path):
    """A checkpoint older than the run says nothing about this run."""
    plan_dir = tmp_path / "p1"
    now = datetime.now(timezone.utc)
    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason=SHUTDOWN_STOP_REASON,
        paused_at=(now - timedelta(hours=1)).isoformat(),
    )

    # The run started an hour after the checkpoint was written: the
    # checkpoint belongs to an earlier run of this plan.
    run_started_at = _naive_local(now)

    assert (
        is_shutdown_interrupted(
            plan_dir, kind="execution", run_started_at=run_started_at
        )
        is False
    )


def test_a_user_requested_checkpoint_is_not_a_shutdown(tmp_path):
    """Somebody pressing stop is not the server going down."""
    plan_dir = tmp_path / "p1"
    paused = datetime.now(timezone.utc)
    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason="user_requested",
        paused_at=paused.isoformat(),
    )

    assert (
        is_shutdown_interrupted(
            plan_dir,
            kind="execution",
            run_started_at=_naive_local(paused - timedelta(minutes=1)),
        )
        is False
    )


def test_a_verification_checkpoint_is_not_an_execution(tmp_path):
    """A verification checkpoint must not wash an execution clean."""
    plan_dir = tmp_path / "p1"
    paused = datetime.now(timezone.utc)
    _write_checkpoint(
        plan_dir,
        kind="verification",
        reason=SHUTDOWN_STOP_REASON,
        paused_at=paused.isoformat(),
    )

    assert (
        is_shutdown_interrupted(
            plan_dir,
            kind="execution",
            run_started_at=_naive_local(paused - timedelta(minutes=1)),
        )
        is False
    )


def test_naive_started_at_is_compared_as_local_time(tmp_path):
    """A naive ``started_at`` is read as local time, not as UTC.

    ``pause_state`` writes ``paused_at`` as an aware UTC stamp and the
    execution state writes ``started_at`` as a naive local one.  Read
    naively against each other the two are the machine's UTC offset
    apart, and every judgement flips.  So the naive stamp is localised
    first -- in *both* directions, which is what pins the reading: a
    run that started before the pause is recognised, a run that
    started after it is not.
    """
    plan_dir = tmp_path / "p1"
    paused = datetime.now(timezone.utc)
    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason=SHUTDOWN_STOP_REASON,
        paused_at=paused.isoformat(),
    )

    assert (
        is_shutdown_interrupted(
            plan_dir,
            kind="execution",
            run_started_at=_naive_local(paused - timedelta(minutes=1)),
        )
        is True
    )
    assert (
        is_shutdown_interrupted(
            plan_dir,
            kind="execution",
            run_started_at=_naive_local(paused + timedelta(minutes=1)),
        )
        is False
    )


def test_an_unparseable_stamp_is_not_recognised(tmp_path):
    """A stamp nobody can parse is treated as a crash."""
    plan_dir = tmp_path / "p1"
    now = datetime.now(timezone.utc)

    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason=SHUTDOWN_STOP_REASON,
        paused_at="not-a-timestamp",
    )
    assert (
        is_shutdown_interrupted(
            plan_dir, kind="execution", run_started_at=now.isoformat()
        )
        is False
    )

    # The run's own stamp is unusable, or missing entirely -- the same
    # answer, because the freshness of the checkpoint cannot be shown.
    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason=SHUTDOWN_STOP_REASON,
        paused_at=now.isoformat(),
    )
    assert (
        is_shutdown_interrupted(
            plan_dir, kind="execution", run_started_at="not-a-timestamp"
        )
        is False
    )
    assert (
        is_shutdown_interrupted(
            plan_dir, kind="execution", run_started_at=None
        )
        is False
    )
    assert (
        is_shutdown_interrupted(
            plan_dir, kind="execution", run_started_at=""
        )
        is False
    )


def test_no_checkpoint_is_not_recognised(tmp_path):
    """A plan with no checkpoint at all crashed."""
    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()

    assert (
        is_shutdown_interrupted(
            plan_dir,
            kind="execution",
            run_started_at=datetime.now(timezone.utc).isoformat(),
        )
        is False
    )


def test_the_reader_seam_can_be_injected(tmp_path):
    """The checkpoint source is a parameter, so a caller may supply its own."""
    plan_dir = tmp_path / "p1"
    paused = datetime.now(timezone.utc)
    seen = []

    def reader(given_plan_dir):
        seen.append(given_plan_dir)
        return {
            "reason": SHUTDOWN_STOP_REASON,
            "kind": "execution",
            "paused_at": paused.isoformat(),
        }

    assert (
        is_shutdown_interrupted(
            plan_dir,
            kind="execution",
            run_started_at=_naive_local(paused - timedelta(minutes=1)),
            reader=reader,
        )
        is True
    )
    assert seen == [plan_dir]