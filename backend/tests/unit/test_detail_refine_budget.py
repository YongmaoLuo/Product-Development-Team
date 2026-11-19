"""A detail refine has its OWN budget, and only the second failure buys it.

Why this file exists
--------------------
``REFINE_WITH_DETAIL`` is the remedy for a failure whose fix is a
targeted refinement rather than a decomposition: the task reached for a
path its declaration never named, and the refiner's answer is precise
("declare this path"). The executor spends the shared refinement budget
(``EXECUTOR_MAX_REFINEMENTS``) on plan restructuring, and that budget
was allowed to veto this refine as well — so a run that had already
refined eight times could never hear the precise answer, and the task
simply stayed failed.

The two budgets are different things:

  * the shared one bounds HOW FAR the run may let the refiner
    restructure the plan;
  * the detail one bounds HOW OFTEN one specific failure may buy one
    targeted refinement.

The detail refine is spent on the SECOND occurrence of the failure: the
first occurrence buys the extra attempt (the answer was wrong, not the
task too big), and only a repeat is evidence that the answer has to
change. Different signatures are counted separately — one refused path
does not spend another refused path's refinement.

What is pinned here
-------------------
* a first occurrence never buys a detail refine;
* the second occurrence does, even when the shared budget is spent;
* the refine is granted once per signature;
* a remedy other than ``REFINE_WITH_DETAIL`` buys nothing;
* the detail refine does not spend the shared budget;
* the executor really calls the refiner when the shared cap is spent.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from failure_classification import (  # noqa: E402
    REFINE_WITH_DETAIL,
    REPORT_FORMAT,
    RETRY_THEN_SPLIT,
    UNDECLARED_WRITE,
    classify_failure,
)


#: The run's project dir -- the worktree the plan is allowed to touch.
PROJECT_DIR = "/tmp/plan-worktree"


@dataclass(frozen=True)
class _StubFailure:
    """A failure that carries everything the executor reads off one.

    The production ``Failure`` carries ``kind`` / ``signature`` /
    ``detail`` and DERIVES its ``remedy``; the occurrence count lives
    BESIDE it, in the executor's ``_note_failure`` table. This stub
    carries all four in one object, so a test can hand the executor a
    failure that says everything about itself.
    """

    kind: str
    remedy: str
    occurrence: int
    signature: str


def _detail_failure(occurrence: int = 2, signature: str = "s1") -> _StubFailure:
    """A ``REFINE_WITH_DETAIL`` failure at the given occurrence."""
    return _StubFailure(
        kind=UNDECLARED_WRITE,
        remedy=REFINE_WITH_DETAIL,
        occurrence=occurrence,
        signature=signature,
    )


def _executor():
    """A bare ``AutonomousAgent`` with only the budget state mirrored.

    ``__new__`` bypasses ``__init__``, so the attributes the budget
    reads are mirrored here from the real initialiser -- exactly the
    way the other refiner tests mirror ``_failure_occurrences`` and
    ``_refinement_count``.
    """
    from agent import AutonomousAgent

    executor = AutonomousAgent.__new__(AutonomousAgent)
    executor.logger = None
    executor._refinement_count = 0
    executor._detail_refinement_count = 0
    executor._detail_refine_by_signature = {}
    return executor


def _executor_with_refiner():
    """An executor whose refiner is the observation point.

    The refiner returns the task list it was handed, so the call is
    observed without the split-application machinery downstream of it
    running.
    """
    from task import SubTask

    executor = _executor()
    executor.project_dir = None
    executor.plan_id = "test-plan"

    task = SubTask(
        id="1",
        title="Test task",
        description="Implement X",
        test_command="echo test",
        status="pending",
    )

    task_manager = MagicMock()
    task_manager.requirement = "Build something small."
    task_manager.tasks = [task]
    executor.task_manager = task_manager

    executor.refiner = MagicMock()
    executor.refiner.refine = MagicMock(
        side_effect=lambda **kwargs: kwargs["tasks"],
    )
    executor._persist_refiner_output = MagicMock()

    return executor, task


# ---------------------------------------------------------------------------
# the budget
# ---------------------------------------------------------------------------
def test_detail_refine_requires_second_occurrence():
    """The first failure buys an extra attempt, not a refinement.

    A first occurrence is evidence that one answer was wrong, not that
    the task is too big -- the executor's own answer to that is one
    more attempt at the answer (see ``_failure_disposition``). Handing
    the refiner a first occurrence would spend the precise remedy on a
    failure that has not repeated yet.
    """
    executor = _executor()

    assert executor._detail_refine_available(_detail_failure(occurrence=1)) is False
    assert executor._detail_refine_available(_detail_failure(occurrence=2)) is True


def test_detail_refine_allowed_when_budget_spent():
    """The shared budget does not govern the detail refine.

    ``EXECUTOR_MAX_REFINEMENTS`` bounds how far the run may let the
    refiner RESTRUCTURE the plan. A detail refine is a targeted fix --
    declare this path -- and it is bounded by its own budget. When the
    shared cap is reached, the run must still be able to hear the one
    precise answer the refiner has.
    """
    from agent import EXECUTOR_MAX_REFINEMENTS

    executor = _executor()
    executor._refinement_count = EXECUTOR_MAX_REFINEMENTS

    assert executor._detail_refine_available(_detail_failure(occurrence=2)) is True


def test_detail_refine_granted_once():
    """One failure buys one refine, not a stream of them.

    The grant is spent when it is handed out: asking a second time for
    the same failure must not produce a second refinement, or the
    "targeted fix" becomes the unbounded tail the shared budget exists
    to stop.
    """
    executor = _executor()
    failure = _detail_failure(occurrence=2)

    assert executor._detail_refine_available(failure) is True
    assert executor._detail_refine_available(failure) is False


def test_other_remedies_not_exempt():
    """Only ``REFINE_WITH_DETAIL`` is exempt.

    Every other remedy keeps the historical behaviour: the failure goes
    through the ordinary path, where the shared budget and the
    occurrence gate decide.
    """
    executor = _executor()
    failure = _StubFailure(
        kind=REPORT_FORMAT,
        remedy=RETRY_THEN_SPLIT,
        occurrence=2,
        signature="s2",
    )

    assert executor._detail_refine_available(failure) is False


# ---------------------------------------------------------------------------
# what the budget is made of
# ---------------------------------------------------------------------------
def test_each_signature_gets_its_own_detail_refine():
    """One refused path does not spend another refused path's refine.

    The budget is counted per signature, so a run that hits two
    different undeclared paths gets one targeted refinement for each.
    """
    executor = _executor()

    first = _detail_failure(occurrence=2, signature="s1")
    second = _detail_failure(occurrence=2, signature="s2")

    assert executor._detail_refine_available(first) is True
    assert executor._detail_refine_available(second) is True
    assert executor._detail_refine_available(first) is False


def test_detail_refine_does_not_spend_the_shared_budget():
    """The two budgets do not share a counter.

    The shared budget bounds plan restructuring; a detail refine is not
    one, so charging it there would let an unrelated budget eat a
    chance it does not govern -- the very bug this file pins.
    """
    executor = _executor()
    executor._refinement_count = 3

    assert executor._detail_refine_available(_detail_failure(occurrence=2)) is True
    assert executor._refinement_count == 3
    assert executor._detail_refinement_count == 1


# ---------------------------------------------------------------------------
# the executor really does it
# ---------------------------------------------------------------------------
def test_detail_refine_reaches_the_refiner_when_the_shared_budget_is_spent():
    """The end-to-end shape: the cap does not veto the refine.

    The production ``Failure`` carries no occurrence -- the executor
    counts occurrences beside it -- so the occurrence arrives as the
    argument the failure tails already pass.
    """
    from agent import EXECUTOR_MAX_REFINEMENTS

    executor, task = _executor_with_refiner()
    executor._refinement_count = EXECUTOR_MAX_REFINEMENTS

    failure = classify_failure(
        f"undeclared_write: {PROJECT_DIR}/backend/app.py",
        project_dir=PROJECT_DIR,
    )
    assert failure.remedy == REFINE_WITH_DETAIL, failure.remedy

    executor._refine_after_failure(
        task,
        "Some impl work.",
        "the write was refused",
        "",
        1,
        failure=failure,
        occurrence=2,
    )

    assert executor.refiner.refine.call_count == 1, (
        "the shared cap must not veto a detail refine"
    )
    assert executor._refinement_count == EXECUTOR_MAX_REFINEMENTS, (
        "the detail refine must not spend the shared budget"
    )


def test_a_first_occurrence_does_not_reach_the_refiner():
    """The first failure is the extra attempt's business, not the refiner's."""
    executor, task = _executor_with_refiner()

    failure = classify_failure(
        f"undeclared_write: {PROJECT_DIR}/backend/app.py",
        project_dir=PROJECT_DIR,
    )

    executor._refine_after_failure(
        task,
        "Some impl work.",
        "the write was refused",
        "",
        1,
        failure=failure,
        occurrence=1,
    )

    assert executor.refiner.refine.call_count == 0
