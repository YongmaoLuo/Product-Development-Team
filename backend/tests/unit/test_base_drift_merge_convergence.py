"""One unfinished base-drift merge task per plan — the loop converges.

The mode gate is gone: a conflict now becomes a merge task in **every**
mode, which is what makes the conflict resolvable at all.  But the same
change opened a hole.  The task id is ``repair-r<round>-<seq>``, so every
round that hits the conflict writes a *new* row — ``repair-r3-01``,
``repair-r4-01``, ``repair-r5-01`` …  A merge that keeps failing (a
conflict the agent cannot resolve on its own) therefore produced one new
task per round, forever.  The plan never converged: it kept dispatching
work that could not succeed, and each round's task was a fresh id, so
nothing ever looked "already handled".

The guard is a read of ``plan_tasks`` **before** the row is written:

* an unfinished base-drift row for this plan (``pending`` /
  ``in_progress`` / ``failed``) means the plan already has a merge in
  flight — no second task is written;
* a **finished** row (``completed`` / ``skipped``) means the merge is
  over; a later drift is a *new* conflict and gets its own task;
* another plan's rows say nothing about this plan.

When the guard fires, the preflight writes no task, so the blocked report
carries **no** task id — and the verification loop, which dispatches a
repair only when it is handed a task id, stops instead of looping.

The properties pinned here:

* a pending row blocks a second task;
* a failed row blocks a second task (a merge that failed is not a merge
  that is over — re-dispatching it every round is the loop);
* a completed row allows a new task;
* another plan's row does not block this plan (negative control);
* with no prior row the task *is* created (positive control — the four
  cases above must not pass because the guard blocks everything).
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import base_drift  # noqa: E402
import verification_agent  # noqa: E402
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
TASK_ID = f"repair-r{ROUND}-01"
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


def test_pending_row_blocks_a_second_task(monkeypatch, tmp_path):
    """A pending merge row means the plan already has a merge in flight.

    The row is ``pending`` — the merge has been queued and nobody has
    finished it.  Writing a second task would give the executor two
    merges for one conflict, and the second one would carry a fresh id,
    so the round would keep finding "work to do" and never converge.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "pending")

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is True
    assert "base_drift_repair_task_id" not in blocked
    assert len(_base_drift_rows(db_path, PLAN_ID)) == 1


def test_failed_row_blocks_a_second_task(monkeypatch, tmp_path):
    """A failed merge row also blocks — that is the loop this guards.

    A failed merge is not a finished merge: the conflict is still there
    and still needs resolving.  But re-dispatching it *every* round with
    a new id is exactly what turned one stuck conflict into an endless
    stream of them.  The row stays open, so the round stops and reports
    instead of piling up tasks.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "failed")

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is True
    assert "base_drift_repair_task_id" not in blocked
    assert len(_base_drift_rows(db_path, PLAN_ID)) == 1


def test_running_row_blocks_a_second_task(monkeypatch, tmp_path):
    """A merge that is *running* is not a finished merge either.

    ``in_progress`` is the status the executor writes while the merge
    agent works.  It is the state the plan is in for the whole time the
    merge runs — the longest window in which a second task could be
    written, and the one where writing it would put two merges on the
    same conflict.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "in_progress")

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is True
    assert "base_drift_repair_task_id" not in blocked
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

    assert blocked["base_drift_repair_task_id"] == TASK_ID
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
    its merge task, with the id the round number gives it.
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

    assert blocked["base_drift_repair_task_id"] == TASK_ID
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
