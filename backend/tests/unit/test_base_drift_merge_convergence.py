"""One unfinished base-drift merge task per plan — the loop converges.

The mode gate is gone: a conflict now becomes a merge task in **every**
mode, which is what makes the conflict resolvable at all.  But the same
change opened a hole.  The task id used to be ``repair-r<round>-<seq>``,
so every round that hits the conflict wrote a *new* row — ``repair-r3-01``,
``repair-r4-01``, ``repair-r5-01`` …  A merge that keeps failing (a
conflict the agent cannot resolve on its own) therefore produced one new
task per round, forever.  The plan never converged: it kept dispatching
work that could not succeed, and each round's task was a fresh id, so
nothing ever looked "already handled".

2026-11-02: the id no longer carries the round — it is ``repair-<seq>``
and the number is allocated **plan-globally** (see
:func:`repair_generator.next_repair_seq_base`).  So the "a later drift
gets its own task" property is now expressed as "a later drift gets the
*next* number", not "a later round gets a different prefix".

The guard is a read of ``plan_tasks`` **before** the row is written:

* an unfinished base-drift row for this plan (``pending`` /
  ``in_progress`` / ``failed``) means the plan already has a merge in
  flight — no second task is written;
* a **finished** row (``completed`` / ``skipped``) means the merge is
  over; a later drift is a *new* conflict and gets its own task;
* another plan's rows say nothing about this plan.

2026-11-12: the guard stopped being a yes/no.  It used to answer "does
this plan own an unfinished merge task?" and the writer turned every
"yes" into **stop the plan**: the round got no task id, the verification
loop stopped, and the plan sat in ``base_drift_conflicts`` forever —
*including* when the task it already owned was queued or running.  Seven
plans did exactly that in the field.

The guard now answers "**which** task, and in what state", and the
writer acts on the state:

* a task that is ``pending`` or ``in_progress`` is **alive** — it is
  queued or running, it *will* be dispatched.  Its id is handed back to
  the loop and no second row is written;
* a task that is ``failed`` is **dead** but the conflict is not: a new
  task is written for it, unless the plan has already had
  :data:`MAX_BASE_DRIFT_MERGE_ATTEMPTS` merge tasks.  That bound is
  what keeps a conflict nobody can resolve from producing a new task
  every round forever;
* no open task at all — a new task is written, as before.

The properties pinned here:

* a pending row is **handed back** — the same id, no second row;
* a running row is **handed back** — the same id, no second row;
* a failed row gets a **bounded** retry — a new row, and a new row only
  while the attempt budget lasts;
* a completed row allows a new task;
* another plan's row does not block this plan (negative control);
* with no prior row the task *is* created (positive control — the cases
  above must not pass because the guard blocks everything).
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import base_drift  # noqa: E402
import verification_agent  # noqa: E402
# ``server`` before ``verification_loop``: the server imports the loop at
# the end of its own body, so the loop has to be loaded through it.
import server  # noqa: E402,F401
import verification_loop  # noqa: E402
from base_drift import (  # noqa: E402
    BASE_DRIFT_VP_ID,
    STATUS_CONFLICTS,
    BaseDriftReport,
)
from config_paths import resolve_state_db_path  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)


PLAN_DIR_NAME = "plan-20261031"
PLAN_ID = PLAN_DIR_NAME
OTHER_PLAN_ID = "plan-20261030"
ROUND = 4
#: The id this plan's merge task gets when the plan owns no repair id
#: yet.  The number is plan-global, so it does not depend on the round.
TASK_ID = "repair-01"
#: The id the merge task gets when the plan already owns ``repair-r3-01``
#: (sequence 1): the allocator hands out the next number, 2.
TASK_ID_AFTER_PRIOR = "repair-02"
PRIOR_TASK_ID = "repair-r3-01"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakePersistence:
    """The persistence surface the preflight touches."""

    def __init__(self) -> None:
        self.events = []
        self.reports = []

    def write_verification_point_log(self, vp_id, event_type, data):
        self.events.append((vp_id, event_type, data))

    def update_report(self, report_data):
        self.reports.append(report_data)


def _orchestrator(tmp_path: Path):
    """An orchestrator carrying only the attributes the preflight reads."""
    orch = verification_agent.Orchestrator.__new__(
        verification_agent.Orchestrator
    )
    orch.project_dir = tmp_path
    orch.plan_dir = tmp_path / PLAN_DIR_NAME
    orch.persistence = _FakePersistence()
    orch._current_round = ROUND
    orch._last_base_drift_report = None
    return orch


def _report(**over) -> BaseDriftReport:
    """A conflicting base-drift report."""
    payload = {
        "status": STATUS_CONFLICTS,
        "base_ref": "origin/main",
        "base_sha": "abc1234",
        "behind": 2,
        "ahead": 0,
        "conflict_files": [{"path": "app.py", "conflict_blocks": 1}],
        "mode": "report",
    }
    payload.update(over)
    return BaseDriftReport(**payload)


def _stub_conflict(monkeypatch, report=None) -> None:
    """Make the check report the given conflict."""
    monkeypatch.setattr(
        base_drift,
        "check_and_merge_base",
        lambda *a, **k: report if report is not None else _report(),
    )


def _use_db(tmp_path: Path, monkeypatch) -> Path:
    """Point the state database at a throwaway file for this test."""
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    assert resolve_state_db_path() == db_path
    return db_path


def _seed_base_drift_row(db_path: Path, plan_id: str, task_id: str, status: str):
    """Write one base-drift merge row for ``plan_id`` in the given state.

    The row is written through the real write path — the same
    :func:`persist_conflict_repair_task` the preflight uses — so the
    guard reads rows that look exactly like the ones it will meet in
    production.  Only the status is set afterwards, because that is
    runtime state the write path does not own.

    The title is a **historical** one, in the shape rows written before
    the base commit joined the title.  It is kept verbatim on purpose:
    the guard under test looks at statuses and ids, not titles, and
    rows left over from before the change are exactly what the guard
    will keep meeting.
    """
    task = {
        "id": task_id,
        "task_group": "repair",
        "failed_vp_id": BASE_DRIFT_VP_ID,
        "round": 3,
        "priority": "P0",
        "files_to_modify": ["app.py"],
        "title": "合并 origin/main：解决 1 个文件的冲突",
        "description": f"plan={plan_id}\n任务类型: base 合并（不是产品代码修复）",
    }
    assert verification_agent.persist_conflict_repair_task(
        plan_id, task, db_path=db_path
    )
    conn = open_db(db_path)
    try:
        PlanTaskRepository(conn).update_task(plan_id, task_id, {"status": status})
    finally:
        conn.close()


def _base_drift_rows(db_path: Path, plan_id: str):
    """Every base-drift row of ``plan_id``, read back through the repository."""
    conn = open_db(db_path)
    try:
        rows = PlanTaskRepository(conn).load_all(plan_id)
    finally:
        conn.close()
    return [
        row for row in rows.values()
        if row["failed_vp_id"] == BASE_DRIFT_VP_ID
    ]


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_pending_row_is_handed_back(monkeypatch, tmp_path):
    """A pending merge row is a merge that **can still run** — hand it back.

    The row is ``pending``: the merge has been queued and nobody has
    finished it.  Writing a second task would give the executor two
    merges for one conflict.  Stopping the plan would be worse: the
    conflict would never be resolved at all, and the plan would sit in
    ``base_drift_conflicts`` forever (that is what seven plans did on
    2026-11-12).  So the *same* id goes back to the loop, and no second
    row is written.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "pending")

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is True
    assert blocked["base_drift_repair_task_id"] == PRIOR_TASK_ID
    assert len(_base_drift_rows(db_path, PLAN_ID)) == 1


def test_failed_row_gets_a_bounded_retry(monkeypatch, tmp_path):
    """A failed merge row is dead — the conflict still needs a task.

    A failed merge is not a finished merge: the conflict is still there
    and still needs resolving.  Re-dispatching it *every* round with a
    new id is what turned one stuck conflict into an endless stream of
    them, so the retry is **bounded**
    (:data:`MAX_BASE_DRIFT_MERGE_ATTEMPTS`).  This plan has had one
    merge task, so it gets another one — and only one.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "failed")

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is True
    assert blocked["base_drift_repair_task_id"] == TASK_ID_AFTER_PRIOR
    assert len(_base_drift_rows(db_path, PLAN_ID)) == 2


def test_running_row_is_handed_back(monkeypatch, tmp_path):
    """A merge that is *running* is the one that must not be duplicated.

    ``in_progress`` is the status the executor writes while the merge
    agent works.  It is the state the plan is in for the whole time the
    merge runs — the longest window in which a second task could be
    written, and the one where writing it would put two merges on the
    same conflict.  The running task's id goes back to the loop.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "in_progress")

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is True
    assert blocked["base_drift_repair_task_id"] == PRIOR_TASK_ID
    assert len(_base_drift_rows(db_path, PLAN_ID)) == 1


def test_completed_row_allows_a_new_task(monkeypatch, tmp_path):
    """A finished merge row does not block — a later drift is a new conflict.

    ``completed`` (or ``skipped``) means the merge is over.  If the base
    moves again afterwards, that is a *new* conflict and it deserves its
    own task; refusing to write one would leave the plan with a
    conflict nobody is asked to resolve.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "completed")

    # The guard asks about the plan *before* the round writes: the
    # finished row must not be counted as unfinished.
    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is False

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert blocked["base_drift_repair_task_id"] == TASK_ID_AFTER_PRIOR
    rows = _base_drift_rows(db_path, PLAN_ID)
    assert len(rows) == 2, rows
    assert rows[1]["status"] == "pending", rows


def test_other_plans_row_does_not_block(monkeypatch, tmp_path):
    """Another plan's merge row says nothing about this plan.

    The guard is per plan: a plan that is waiting for its own merge must
    not silence a *different* plan that is waiting for its own.  Rows
    are read per ``plan_id``, so the other plan's open row is invisible
    from here.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, OTHER_PLAN_ID, PRIOR_TASK_ID, "pending")

    # The other plan's row is open — for that plan.  This plan has
    # nothing open, so its own conflict still gets a task.
    assert (
        verification_agent.has_open_base_drift_merge_task(OTHER_PLAN_ID) is True
    )
    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is False

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert blocked["base_drift_repair_task_id"] == TASK_ID
    assert len(_base_drift_rows(db_path, PLAN_ID)) == 1


def test_no_prior_row_creates_the_task(monkeypatch, tmp_path):
    """With no prior row the task *is* created.

    The positive control: the four cases above must not pass because the
    guard blocks everything.  A plan that has never seen a conflict gets
    its merge task, with the first number of the plan's repair sequence.
    """
    db_path = _use_db(tmp_path, monkeypatch)

    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is False

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert blocked["base_drift_repair_task_id"] == TASK_ID
    rows = _base_drift_rows(db_path, PLAN_ID)
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "pending", rows


def test_skipped_row_allows_a_new_task(monkeypatch, tmp_path):
    """``skipped`` is finished too — a skipped merge does not block.

    ``skipped`` is the other terminal status: the merge was not done,
    but it is not pending either, so the plan is not waiting for it.  A
    later drift is a new conflict and gets its own task.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "skipped")

    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is False

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert blocked["base_drift_repair_task_id"] == TASK_ID_AFTER_PRIOR
    assert len(_base_drift_rows(db_path, PLAN_ID)) == 2


def test_query_failure_only_warns(monkeypatch, tmp_path, caplog):
    """A read that fails warns and lets the round go on.

    The guard is a read.  If the read fails — a locked database, a
    corrupted file — the round must still hand the conflict to somebody:
    refusing to write a task because a *read* failed would strand the
    conflict with nobody to resolve it, which is the very failure the
    mode gate used to cause.  So the failure is written down as a
    warning and the round continues as if there were no prior row.
    """
    db_path = _use_db(tmp_path, monkeypatch)

    def boom(self, plan_id):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(PlanTaskRepository, "load_all", boom)

    with caplog.at_level("WARNING", logger="verification_agent"):
        assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is False

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "verification_agent"
    ]
    assert any("could not read" in message for message in warnings), warnings

    # And the round is not blocked by it: the conflict still gets a task.
    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert blocked["base_drift_repair_task_id"] == TASK_ID


# ---------------------------------------------------------------------------
# The guard's answer decides what the round does — not just "stop"
# ---------------------------------------------------------------------------
#
# The guard used to be a yes/no, and every "yes" turned into "stop the
# plan": the round got no task id, the verification loop stopped, and the
# plan sat in ``base_drift_conflicts`` forever — *including* when the task
# it already owned was queued or running.  Seven plans did exactly that
# on 2026-11-12.
#
# These cases drive the **production entry point**
# (``Orchestrator._run_base_drift_preflight``) with the plan's rows and
# the task write stubbed out: no database, no service, no executor.  The
# assertion that matters is the one the verification loop reads —
# ``base_drift_repair_task_id`` — because that is what turns "stop" into
# "repair".


def _base_drift_row(status: str):
    """One base-drift merge row, in the shape the guard reads."""
    return {"failed_vp_id": BASE_DRIFT_VP_ID, "status": status}


def _stub_plan_rows(monkeypatch, rows):
    """Answer the plan's task rows from a dict — no database involved."""
    def fake_read(plan_id, *, db_path=None):
        return dict(rows), None

    monkeypatch.setattr(verification_agent, "_read_plan_task_rows", fake_read)
    return fake_read


def _stub_writes(monkeypatch):
    """Record every task the round writes; write nothing."""
    written = []

    def fake_persist(plan_id, task):
        written.append((plan_id, task))
        return True

    monkeypatch.setattr(
        verification_agent, "persist_conflict_repair_task", fake_persist
    )
    return written


def test_running_task_is_handed_back_and_no_second_row_is_written(
    monkeypatch, tmp_path,
):
    """A merge that is *running* is handed back to the executor.

    The round finds a merge task that is already in flight.  It must not
    write a second one (two merges on one conflict), and it must not stop
    the plan either — stopping it means the conflict is never resolved
    and the plan is dead in ``base_drift_conflicts`` for good.  So the
    **same** id goes back to the loop, which then dispatches the repair.
    """
    _stub_plan_rows(monkeypatch, {"repair-01": _base_drift_row("in_progress")})
    written = _stub_writes(monkeypatch)

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert blocked["base_drift_repair_task_id"] == "repair-01"
    assert written == []
    assert verification_loop._base_drift_round_action(blocked) == "repair"


def test_pending_task_is_handed_back_and_no_second_row_is_written(
    monkeypatch, tmp_path,
):
    """A merge that is *queued* is handed back to the executor too.

    ``pending`` is the same situation one step earlier: the task exists,
    it has not run, and it *will* run.  Handing it back is what lets the
    loop dispatch it; writing a second row would put two merges on one
    conflict.
    """
    _stub_plan_rows(monkeypatch, {"repair-01": _base_drift_row("pending")})
    written = _stub_writes(monkeypatch)

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert blocked["base_drift_repair_task_id"] == "repair-01"
    assert written == []
    assert verification_loop._base_drift_round_action(blocked) == "repair"


def test_failed_task_is_retried_while_the_attempt_budget_lasts(
    monkeypatch, tmp_path,
):
    """A merge that *failed* gets a new task — a bounded number of times.

    The failed row is dead: the executor is done with it and it will
    never run again.  The conflict it was written for is still there, so
    the plan gets a **new** task under a new id.  The retry is bounded by
    :data:`MAX_BASE_DRIFT_MERGE_ATTEMPTS`, so a conflict nobody can
    resolve cannot produce a new task every round forever.
    """
    _stub_plan_rows(monkeypatch, {"repair-01": _base_drift_row("failed")})
    written = _stub_writes(monkeypatch)

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert [task["id"] for _plan_id, task in written] == ["repair-02"]
    assert blocked["base_drift_repair_task_id"] == "repair-02"
    assert verification_loop._base_drift_round_action(blocked) == "repair"


def test_failed_task_is_not_retried_past_the_attempt_budget(
    monkeypatch, tmp_path,
):
    """Past the budget the plan stops — and that is the *only* way it stops.

    Three merge tasks have failed for this plan.  A fourth would be the
    endless stream the guard exists to stop, so the round writes nothing
    and hands the loop no task id — and the loop stops the plan with
    ``base_drift_conflicts``.  This is the deliberate, bounded end of the
    road; every other state keeps the plan moving.
    """
    rows = {
        "repair-01": _base_drift_row("failed"),
        "repair-02": _base_drift_row("failed"),
        "repair-03": _base_drift_row("failed"),
    }
    assert len(rows) == verification_agent.MAX_BASE_DRIFT_MERGE_ATTEMPTS
    _stub_plan_rows(monkeypatch, rows)
    written = _stub_writes(monkeypatch)

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert written == []
    assert "base_drift_repair_task_id" not in blocked
    assert verification_loop._base_drift_round_action(blocked) == "stop"


def test_no_open_task_writes_a_new_task_as_before(monkeypatch, tmp_path):
    """With no open task the round writes a task — the behaviour of today.

    The positive control: the four cases above must not pass because the
    guard blocks everything.  A plan that has never seen a conflict gets
    its merge task, with the first number of the plan's repair sequence.
    """
    _stub_plan_rows(monkeypatch, {})
    written = _stub_writes(monkeypatch)

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert [task["id"] for _plan_id, task in written] == ["repair-01"]
    assert blocked["base_drift_repair_task_id"] == "repair-01"
    assert verification_loop._base_drift_round_action(blocked) == "repair"


def test_find_open_prefers_the_running_task(monkeypatch):
    """A plan carrying several open merge tasks hands back the running one.

    The same conflict can leave a plan with a failed row *and* a running
    one.  The running task is the one the executor is working on, so it
    is the one to hand back.
    """
    _stub_plan_rows(monkeypatch, {
        "repair-01": _base_drift_row("failed"),
        "repair-02": _base_drift_row("pending"),
        "repair-03": _base_drift_row("in_progress"),
    })

    found = verification_agent.find_open_base_drift_merge_task(PLAN_ID)

    assert found == ("repair-03", "in_progress")


def test_find_open_ignores_finished_tasks(monkeypatch):
    """A finished merge task is not open — the guard says so, as before."""
    _stub_plan_rows(monkeypatch, {"repair-01": _base_drift_row("completed")})

    assert verification_agent.find_open_base_drift_merge_task(PLAN_ID) is None
    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is False


def test_count_counts_every_base_drift_task_of_the_plan(monkeypatch):
    """The attempt counter counts the plan's base-drift tasks, whatever state.

    A completed merge is still a merge this plan had, and it still
    happened before the conflict that is being retried.  Rows that are
    not base-drift merges (an ordinary verification task) are not
    counted.
    """
    _stub_plan_rows(monkeypatch, {
        "repair-01": _base_drift_row("completed"),
        "repair-02": _base_drift_row("failed"),
        "vp-01": {"failed_vp_id": "vp-01", "status": "pending"},
    })

    assert verification_agent.count_base_drift_merge_tasks(PLAN_ID) == 2
