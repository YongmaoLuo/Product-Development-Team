"""Which way a base-drift-blocked round goes: ``repair`` or ``stop``.

Background
----------
A round the base-drift check blocked by a conflict carries the stop
reason ``base_drift_conflicts`` and the conflict's own evidence.  What
the verification loop does with such a round is decided by one thing
only: **did the conflict become a task?**

* A task id — the conflict went through the repair channel that
  already exists, so the round hands the plan to the executor and the
  merge gets resolved: ``"repair"``.
* No task id — the conflict was never turned into work, so the round
  ends here and writes down why: ``"stop"``.

That distinction is the whole of the failure convergence.  The task id
is ``repair-r<round>-<seq>``, so before the convergence guard every
round that hit the conflict wrote a **new** row — ``repair-r3-01``,
``repair-r4-01``, ``repair-r5-01`` …  A merge that cannot succeed (a
conflict the agent cannot resolve on its own) therefore produced one
task per round, forever: the plan never converged, it kept dispatching
work that could not succeed.

The guard reads ``plan_tasks`` *before* the row is written — an
unfinished base-drift row for the plan means no second task is written,
so the blocked report carries **no** task id, and the mapping below
says ``"stop"``.  The failure is reported once instead of being
re-dispatched forever.

So the mapping is the only place where "report the failure once"
becomes "stop".  If it drifts — if a report with no task id were
dispatched as a repair, or a report with a task id were dropped — the
loop this guard was written to close comes back, silently.

TDD contract — 5 test cases
---------------------------
1. ``test_task_id_means_repair`` — a conflict that became a task is
   handed to the executor.
2. ``test_missing_task_id_means_stop`` — a conflict that became no
   task ends the round.
3. ``test_blank_task_id_means_stop`` — an empty or blank id is **no**
   id: there is nothing to repair with, and a repair dispatch with
   nothing behind it sends the executor after an empty queue.
4. ``test_non_base_drift_report_is_not_ours`` — a report whose
   ``stop_reason`` is anything else is not this function's business.
5. ``test_failed_merge_converges_to_stop`` — round 3's failed merge,
   compressed by the guard into a report with no task id, stops: the
   failure is reported once.

Boundary conditions
-------------------
* A task id that is empty, blank or missing is no task.
* A report whose ``stop_reason`` differs is not ours: ``None``.
* A report that is not a dict at all: ``None``.
* Read-only — the module import starts no thread and the cases write
  no database row; they are pure function calls over report dicts.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ``backend/`` on sys.path so ``import server`` works no matter which
# entry point runs the suite, and the repository root so the
# ``backend.framework`` package resolves.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent
for _path in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import base_drift  # noqa: E402
import server  # noqa: E402,F401  — seeds sys.modules["server"] for the loop
import verification_loop  # noqa: E402
import pytest  # noqa: E402


#: The stop reason a base-drift-blocked round carries.
STOP_REASON = verification_loop.BASE_DRIFT_CONFLICTS_STOP_REASON


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _blocked_report(**over):
    """The report a round blocked by a base-drift conflict carries.

    The payload mirrors what the loop writes down when the base-drift
    check blocks the round: the stop reason, the execution profile and
    the conflict's own evidence.  ``over`` replaces any of it.
    """
    payload = {
        "overall_status": "BLOCKED",
        "stop_reason": STOP_REASON,
        "execution_profile": {"mode": "base_drift_blocked"},
        "base_drift_conflicts": {"status": "conflicts"},
    }
    payload.update(over)
    return payload


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


def test_task_id_means_repair():
    """A conflict that became a task is handed to the executor.

    The repair channel already exists and already knows how to run a
    base merge.  A report that carries a task id has been through that
    channel, so the round dispatches: the merge gets a second pair of
    hands and the conflict gets resolved.
    """
    report = _blocked_report(base_drift_repair_task_id="repair-r3-01")

    assert verification_loop._base_drift_round_action(report) == "repair"


def test_missing_task_id_means_stop():
    """A conflict that became no task ends the round.

    There is no work to hand over, so the round writes down why it is
    blocked and stops.  This is the branch the convergence guard
    produces: the plan already has an unfinished merge, so no second
    task was written, so the report carries no id.
    """
    report = _blocked_report()

    assert "base_drift_repair_task_id" not in report
    assert verification_loop._base_drift_round_action(report) == "stop"


def test_blank_task_id_means_stop():
    """An empty or blank id is **no** id.

    A whitespace-only id looks like a task but names none.  Treating it
    as a task would dispatch a repair against an empty queue — the
    executor would be sent after a task that does not exist.
    """
    for blank in ("", "   ", "\t\n"):
        report = _blocked_report(base_drift_repair_task_id=blank)

        assert verification_loop._base_drift_round_action(report) == "stop", blank


def test_non_base_drift_report_is_not_ours():
    """A report that is not a base-drift conflict is none of our business.

    A round that ran and failed, a round blocked by its services, a
    round that merely gave up: each of them is someone else's verdict.
    Claiming one of them would make this function dispatch a base merge
    for a round that never had a conflict.
    """
    assert (
        verification_loop._base_drift_round_action({"overall_status": "FAILED"})
        is None
    )

    # A task id does not make a foreign report ours — the stop reason
    # is what identifies the report.
    foreign = _blocked_report(
        stop_reason="verification_failed",
        base_drift_repair_task_id="repair-r3-01",
    )
    assert verification_loop._base_drift_round_action(foreign) is None


@pytest.mark.parametrize(
    "report",
    [None, [], "base_drift_conflicts", 42],
    ids=["none", "list", "string", "int"],
)
def test_non_dict_report_is_not_ours(report):
    """A report that is not a dict is not a report at all."""
    assert verification_loop._base_drift_round_action(report) is None


def test_failed_merge_converges_to_stop():
    """Round 3's failed merge is compressed into a report with no task id.

    The guard reads ``plan_tasks`` before the round writes: an
    unfinished base-drift row for this plan means no second task is
    written.  So the blocked report still carries the conflict's
    evidence — the round knows exactly what blocked it — but it carries
    **no** task id, and the round therefore stops.

    That is the convergence: a merge that failed is reported once, not
    re-dispatched under a fresh id on every following round.  The
    round-3 report (with its id, ``repair-r3-01``) would have said
    ``"repair"``; the compressed one says ``"stop"``.
    """
    compressed = _blocked_report()

    # The conflict evidence survived the compression …
    blocked = verification_loop._base_drift_blocked_report(compressed)
    assert blocked is not None
    assert blocked["status"] == base_drift.STATUS_CONFLICTS

    # … and the round stops rather than dispatching a second merge.
    assert verification_loop._base_drift_round_action(compressed) == "stop"

    # The uncompressed round-3 report is the contrast: with a task id
    # it did dispatch.
    round_three = _blocked_report(base_drift_repair_task_id="repair-r3-01")
    assert verification_loop._base_drift_round_action(round_three) == "repair"


def test_stop_reason_is_the_base_drift_event_name():
    """The report's stop reason is the check's own event name.

    The stopped round and the event log have to say the same word: the
    check reports ``conflicts`` under the event name
    ``base_drift_conflicts``, and the round that is blocked by it
    carries that name as its stop reason.  If the two ever drifted
    apart, every base-drift report would read as "not ours" and the
    loop would neither repair nor stop.
    """
    assert STOP_REASON == "base_drift_conflicts"
    assert (
        STOP_REASON
        == base_drift.EVENT_BY_STATUS[base_drift.STATUS_CONFLICTS]
    )
