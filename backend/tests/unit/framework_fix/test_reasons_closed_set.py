"""The scheduler's reason vocabulary once the framework-fix lane is in it.

Background
----------
``plan_scheduler.policy`` answers three questions — may this plan start
now, who gets interrupted, what has to go — and every answer carries a
word from :data:`plan_scheduler.policy.REASONS`.  The set is **closed**:
the panel renders the words, the dry run records them and the API
publishes them, and a word outside the set is a bug in the policy rather
than a new situation to describe in a new word.

The framework-fix lane adds five situations of its own — a plan that
runs first because it is a framework fix, one that was pushed aside for
one, one that is protected from being pushed aside, one waiting for its
repository's merge slot, and one whose wait has gone on long enough to
look like a deadlock — so the closed set has to grow before anything
else can name them.  This file pins that growth:

* the five words are **in** the set;
* the eight words the set had before are **still** in it — the lane adds
  words, it does not trade old ones away;
* the lane's priority sits **ahead** of an ordinary plan's, so a
  framework-fix plan always stands in front;
* the phases that count against the active quota are the phases in which
  a plan holds compute — a waiting plan holds nothing and counts for
  nothing.
"""

from __future__ import annotations

from plan_scheduler.policy import (
    COUNTED_PHASES,
    FRAMEWORK_FIX_PRIORITY,
    ORDINARY_MIN_PRIORITY,
    REASONS,
)

#: The eight words the vocabulary carried before the framework-fix lane.
ORIGINAL_REASONS = frozenset(
    {
        "running",
        "waiting_for_slot",
        "blocked_by_capacity",
        "workspace_busy",
        "due_later",
        "hitl_pending",
        "paused",
        "over_capacity",
    }
)

#: The five words the framework-fix lane adds to the vocabulary.
FRAMEWORK_FIX_REASONS = frozenset(
    {
        "framework_fix_priority",
        "preempted_by_framework_fix",
        "protected_framework_fix",
        "waiting_merge_slot",
        "scheduler_deadlock_suspected",
    }
)


def test_closed_set_extended() -> None:
    """The five framework-fix words are in the closed set.

    Each of them names a situation the lane can be in: it runs first
    because it is a framework fix, it was pushed aside for one, it is
    protected from being pushed aside, it is waiting for the merge slot
    of its repository, or it has waited so long that the tick treats the
    wait as a deadlock.
    """
    assert FRAMEWORK_FIX_REASONS <= REASONS, sorted(
        FRAMEWORK_FIX_REASONS - REASONS
    )


def test_old_reasons_kept() -> None:
    """The eight words the vocabulary had are still in it.

    The lane adds words; it does not trade old ones away.  A word that
    disappeared would leave the panel, the dry run and the API drawing
    something the policy no longer answers with.
    """
    assert ORIGINAL_REASONS <= REASONS, sorted(ORIGINAL_REASONS - REASONS)


def test_priority_constant() -> None:
    """The framework-fix priority stands ahead of an ordinary plan's.

    Smaller runs first — the queue sorts by ascending priority — so the
    lane's own plans have to sit on a level below the ordinary ones.
    The value is the smallest there is and it is reserved: the level a
    framework-fix plan occupies is :data:`FRAMEWORK_FIX_PRIORITY`, the
    level an ordinary plan occupies is :data:`ORDINARY_MIN_PRIORITY`,
    and the first is strictly ahead of the second.
    """
    assert isinstance(FRAMEWORK_FIX_PRIORITY, int)
    assert FRAMEWORK_FIX_PRIORITY == 0
    assert isinstance(ORDINARY_MIN_PRIORITY, int)
    assert FRAMEWORK_FIX_PRIORITY < ORDINARY_MIN_PRIORITY


def test_closed_set_is_thirteen_words() -> None:
    """The set holds the eight it had and the five it gained — and no more.

    A closed set is closed: it is exactly the words the policy may
    answer with, so a thirteenth word that is not one of the thirteen
    would be a word the panel cannot draw.
    """
    assert len(REASONS) == len(ORIGINAL_REASONS) + len(FRAMEWORK_FIX_REASONS)
    assert REASONS == ORIGINAL_REASONS | FRAMEWORK_FIX_REASONS


def test_counted_phases_are_the_working_phases() -> None:
    """Only the phases in which a plan holds compute count against the quota.

    A plan that is executing, or that a verification round is working on,
    holds a worker — and a plan that is winding down still holds its
    worker, so it counts too.  A plan that is *waiting* holds nothing:
    queued for its turn, parked for provider quota, waiting for a human.
    None of those count, or the quota would be spent by plans that are
    not running.
    """
    assert isinstance(COUNTED_PHASES, frozenset)
    assert all(isinstance(phase, str) for phase in COUNTED_PHASES)

    assert "executing" in COUNTED_PHASES
    assert "verification_running" in COUNTED_PHASES
    assert "verification_repairing" in COUNTED_PHASES
    assert "user_stopping" in COUNTED_PHASES

    assert "queued" not in COUNTED_PHASES
    assert "waiting_for_quota" not in COUNTED_PHASES
    assert "verification_plan_review" not in COUNTED_PHASES
