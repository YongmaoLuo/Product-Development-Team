"""A recorded ``failure_reason`` must survive a status reset.

Background (2026-09-13, plan ``2026-09-04 plan``):
the inline-spec review block path records the review verdict with
``record_task_failure`` and then reverts the task to ``pending`` so the
next run picks it up. Both calls go through ``runtime_overrides`` — and
``update_task_status`` used to *replace* the entry wholesale, dropping
the ``failure_reason`` key that ``record_task_failure`` had just written.

The loss was invisible in-process (``task.failure_reason`` on the
in-memory ``SubTask`` still held the text) and only surfaced on the next
``load_tasks`` rehydrate, which rebuilds each task from
``runtime_overrides`` — the operator-visible reason vanished exactly when
someone went looking for why a task was bounced. That is what made an
earlier plan's failures unreadable from the progress card.

Contract pinned here:

  1. ``record_task_failure`` → ``update_task_status(pending)`` keeps the
     ``failure_reason`` in ``runtime_overrides``.
  2. The reset still wins on the fields it owns (``status``,
     ``updated_time``).
  3. A task with no prior override gains no invented ``failure_reason``.
  4. Repeat resets do not accumulate or drop the reason.
  5. A falsy (``None`` / ``""``) prior reason is not carried over.

2026-09-16 addendum: preservation stops at ``completed``.

The executor now feeds ``task.failure_reason`` into the prompt on the
first attempt of every re-dispatched task (``agent.py``
``_build_prior_failure_block``). That makes a stale reason actively
harmful: a task that failed, then succeeded, and is later re-dispatched
for any reason would be told to fix a failure that no longer exists and
might "fix" working code. So ``completed`` — a terminal, success state —
drops the reason instead of carrying it. The behaviours pinned in
1-5 are unaffected: none of them ends in ``completed``.
"""

import sys
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from task import SubTask  # noqa: E402


def _make_task(task_id: str = "11-4") -> SubTask:
    return SubTask(
        id=task_id,
        title="example",
        description="example description",
        test_command="pytest -q",
        files_to_modify=["foo.py"],
    )


def _bare_manager(task_id: str = "11-4"):
    """A ``TaskManager`` with no disk/SQLite side effects.

    Bypasses ``__init__`` the same way ``test_task_manager_update_commit_sha``
    does: this test is about the in-memory mirror, so ``save_tasks`` and
    ``_persist_status_to_sqlite`` are stubbed rather than exercised.
    """
    from task_manager import TaskManager

    tm = TaskManager.__new__(TaskManager)
    tm.tasks = [_make_task(task_id)]
    tm.runtime_overrides = {}
    tm.tasks_file = Path("/tmp/nonexistent/tasks.json")
    tm.save_tasks = lambda: None
    return tm


def test_failure_reason_survives_status_reset_to_pending():
    """The load-bearing case: block → revert → reason still readable."""
    tm = _bare_manager("R1-5")
    with patch.object(
        type(tm), "_persist_status_to_sqlite", lambda self, tid, st: None
    ):
        tm.record_task_failure("R1-5", "inline spec review: missing test_command")
        tm.update_task_status("R1-5", "pending")

    override = tm.runtime_overrides["R1-5"]
    assert override["status"] == "pending", (
        "the reset must win on status — the task is scheduled again"
    )
    assert override["failure_reason"] == (
        "inline spec review: missing test_command"
    ), (
        "the recorded failure_reason was erased by the status reset; a "
        "later load_tasks rehydrate rebuilds the task from "
        "runtime_overrides, so the operator loses the reason"
    )


def test_status_reset_still_owns_status_and_updated_time():
    """Preservation must not turn ``update_task_status`` into a no-op."""
    tm = _bare_manager("1-1")
    stale = "1999-01-01T00:00:00"
    with patch.object(
        type(tm), "_persist_status_to_sqlite", lambda self, tid, st: None
    ):
        tm.record_task_failure("1-1", "boom")
        tm.runtime_overrides["1-1"]["updated_time"] = stale
        tm.update_task_status("1-1", "in_progress")

    override = tm.runtime_overrides["1-1"]
    assert override["status"] == "in_progress"
    assert override["updated_time"] != stale, (
        "updated_time must be refreshed by the reset, not carried over"
    )
    assert override["failure_reason"] == "boom"


def test_no_prior_override_gains_no_failure_reason():
    """A plain status change must not fabricate a ``failure_reason``."""
    tm = _bare_manager("2-1")
    with patch.object(
        type(tm), "_persist_status_to_sqlite", lambda self, tid, st: None
    ):
        tm.update_task_status("2-1", "in_progress")

    assert tm.runtime_overrides["2-1"] == {
        "status": "in_progress",
        "updated_time": tm.tasks[0].updated_time,
    }
    assert "failure_reason" not in tm.runtime_overrides["2-1"], (
        "the override map drives the progress API's task dict; an "
        "invented reason would show a failure that never happened"
    )


def test_repeated_resets_keep_the_reason_stable():
    """Block/revert cycles must not accumulate or drop the reason."""
    tm = _bare_manager("3-1")
    with patch.object(
        type(tm), "_persist_status_to_sqlite", lambda self, tid, st: None
    ):
        tm.record_task_failure("3-1", "first reason")
        tm.update_task_status("3-1", "pending")
        tm.update_task_status("3-1", "in_progress")
        tm.update_task_status("3-1", "pending")

    assert tm.runtime_overrides["3-1"]["failure_reason"] == "first reason"


def test_falsy_prior_reason_is_not_carried_over():
    """Only a *recorded* reason is preserved — not a blank placeholder."""
    tm = _bare_manager("4-1")
    tm.runtime_overrides["4-1"] = {"status": "failed", "failure_reason": ""}
    with patch.object(
        type(tm), "_persist_status_to_sqlite", lambda self, tid, st: None
    ):
        tm.update_task_status("4-1", "pending")

    assert "failure_reason" not in tm.runtime_overrides["4-1"]


# ---------------------------------------------------------------------------
# 2026-09-16: preservation stops at ``completed``
# ---------------------------------------------------------------------------


def test_completion_clears_the_in_memory_failure_reason():
    """A succeeded task must not keep a reason for the prompt injector.

    ``_build_prior_failure_block`` reads ``task.failure_reason``; if a
    resolved failure stayed attached, the next re-dispatch would be
    handed a problem that was already fixed.
    """
    tm = _bare_manager("5-1")
    with patch.object(
        type(tm), "_persist_status_to_sqlite", lambda self, tid, st: None
    ):
        tm.record_task_failure("5-1", "old failure")
        assert tm.tasks[0].failure_reason == "old failure"
        tm.update_task_status("5-1", "completed")

    assert tm.tasks[0].failure_reason is None, (
        "a completed task still carries a failure_reason; the executor "
        "would inject it into a later re-dispatch as if unfixed"
    )


def test_completion_drops_the_reason_from_runtime_overrides():
    """The override map drives the next ``load_tasks`` rehydrate."""
    tm = _bare_manager("5-2")
    with patch.object(
        type(tm), "_persist_status_to_sqlite", lambda self, tid, st: None
    ):
        tm.record_task_failure("5-2", "old failure")
        tm.update_task_status("5-2", "completed")

    override = tm.runtime_overrides["5-2"]
    assert override["status"] == "completed"
    assert "failure_reason" not in override, (
        "the completed override still carries a failure_reason; the "
        "next rehydrate rebuilds the task from this map and would "
        "resurrect a resolved failure"
    )


def test_reset_to_pending_after_completion_does_not_resurrect():
    """The full lifecycle: fail → succeed → re-scheduled."""
    tm = _bare_manager("5-3")
    with patch.object(
        type(tm), "_persist_status_to_sqlite", lambda self, tid, st: None
    ):
        tm.record_task_failure("5-3", "old failure")
        tm.update_task_status("5-3", "completed")
        tm.update_task_status("5-3", "pending")

    assert "failure_reason" not in tm.runtime_overrides["5-3"], (
        "the resolved failure came back when the task was re-scheduled"
    )
    assert tm.tasks[0].failure_reason is None


def test_a_new_failure_after_completion_is_recorded_again():
    """Clearing on success must not disable failure recording."""
    tm = _bare_manager("5-4")
    with patch.object(
        type(tm), "_persist_status_to_sqlite", lambda self, tid, st: None
    ):
        tm.record_task_failure("5-4", "first failure")
        tm.update_task_status("5-4", "completed")
        tm.record_task_failure("5-4", "second failure")

    assert tm.tasks[0].failure_reason == "second failure"
    assert tm.runtime_overrides["5-4"]["failure_reason"] == "second failure"


def test_pending_reset_still_preserves_despite_the_completion_rule():
    """Guard the 2026-09-13 contract against the new ``completed`` rule."""
    tm = _bare_manager("5-5")
    with patch.object(
        type(tm), "_persist_status_to_sqlite", lambda self, tid, st: None
    ):
        tm.record_task_failure("5-5", "inline spec review: missing test_command")
        tm.update_task_status("5-5", "pending")

    assert (
        tm.runtime_overrides["5-5"]["failure_reason"]
        == "inline spec review: missing test_command"
    )
