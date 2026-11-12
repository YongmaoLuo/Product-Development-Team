"""A **voided** base-drift merge row does not wedge the convergence guard.

The convergence guard (:func:`verification_agent.has_open_base_drift_merge_task`)
asks one question before the round writes a merge task: *does this plan
already carry an unfinished base-drift merge task?*  A row that is not
finished blocks the write, the round gets no task id, and the loop stops
— that is the loop the guard was built to stop (a conflict re-dispatched
under a fresh id every round, forever).

``voided`` is not "unfinished".  A voided task was **cancelled**: its
premise was revoked (a verification reset discarded the verdicts it was
generated from, or a human ran the ``obsolete`` delta action) and the
dispatcher will never schedule it again.  There is nobody left who is
going to resolve that conflict through the voided row.

Reading a voided row as unfinished therefore wedged the plan completely:
the guard said "a merge is already in flight", the round wrote no task,
the loop stopped, and every single round died in
``base_drift_conflicts`` — forever.  Three plans in the field did exactly
that for two days (``repair-107``, ``repair-03``, ``repair-04``); two of
them only recovered through a manual resync.

``failed`` is a different animal and stays unfinished **on purpose**: a
failed merge is not a finished merge.  The conflict is still there.  It
is not obeyed, though — the writer retries it, a **bounded** number of
times (:data:`MAX_BASE_DRIFT_MERGE_ATTEMPTS`), so a conflict nobody can
resolve cannot produce a new task every round forever.

2026-11-12: the guard stopped being a yes/no.  It used to answer "does
this plan own an unfinished merge task?" and every "yes" turned into
"stop the plan" — the round got no task id and the verification loop
stopped, so a plan whose merge task was queued or running sat in
``base_drift_conflicts`` forever without ever resolving the conflict.
The guard now answers "**which** task, and in what state", and the
writer acts on the state: a task that can still run is handed back to
the executor, a task that died is retried a bounded number of times.

The properties pinned here:

* a ``voided`` base-drift row does not block — a new task is written and
  the round's action is ``"repair"``;
* a ``failed`` base-drift row is retried — a new task, action ``"repair"``;
* a ``pending`` base-drift row is handed back — the same id, no second
  row, action ``"repair"``.
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


PLAN_DIR_NAME = "plan-20261108"
PLAN_ID = PLAN_DIR_NAME
ROUND = 4
#: The id of the row the plan already owns — the one whose status each
#: test varies.  It is a *historical* id (the round number is part of
#: it), because that is the shape the rows left behind by the old
#: allocator have, and those are exactly the rows the guard keeps
#: meeting.
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
    :func:`verification_agent.persist_conflict_repair_task` the preflight
    uses — so the guard reads rows that look exactly like the ones it
    meets in production.  Only the status is set afterwards, because that
    is runtime state the write path does not own.
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
        dict(row, task_id=task_id)
        for task_id, row in rows.items()
        if row["failed_vp_id"] == BASE_DRIFT_VP_ID
    ]


# ---------------------------------------------------------------------------
# a) A voided row is terminal — the round writes a new task
# ---------------------------------------------------------------------------


def test_voided_row_does_not_block_a_new_task(monkeypatch, tmp_path):
    """A cancelled merge is not a merge in flight — the round goes on.

    The voided row was written by a human action or a verification reset;
    the conflict it belonged to is still live, and nobody is going to
    resolve it through a task that will never run.  So the guard must
    report "no unfinished merge task", the round must write a fresh
    merge task, and the loop's action must be ``"repair"`` — not
    ``"stop"``, which is what wedged these plans.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "voided")

    # The guard's own answer, before the round asks.
    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is False

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    # The preflight hands the round a task id ...
    task_id = blocked.get("base_drift_repair_task_id")
    assert task_id, blocked
    assert str(task_id).startswith("repair-"), task_id

    # ... and the loop reads that id as "there is a repair to dispatch".
    assert verification_loop._base_drift_round_action(blocked) == "repair"

    rows = _base_drift_rows(db_path, PLAN_ID)
    assert len(rows) == 2, rows
    # The voided row is still there, untouched, and the new one is
    # queued.
    assert sorted(row["status"] for row in rows) == ["pending", "voided"], rows
    assert [row for row in rows if row["status"] == "pending"][0]["task_id"] == (
        task_id
    )


def test_voided_row_lets_persist_write_a_new_task(monkeypatch, tmp_path):
    """The same thing one level down: the writer gets an id, not ``None``.

    :meth:`Orchestrator._persist_base_drift_repair_task` returns the new
    task id, and ``None`` when the plan already carries an unfinished
    merge task.  A voided row must not take that branch: ``None`` is
    exactly what made the round stop forever.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "voided")

    orch = _orchestrator(tmp_path)

    task_id = orch._persist_base_drift_repair_task(_report())

    assert task_id is not None, "a voided row must not block a new task"
    assert str(task_id).startswith("repair-"), task_id
    assert len(_base_drift_rows(db_path, PLAN_ID)) == 2


# ---------------------------------------------------------------------------
# b) A failed row is unfinished — but it is retried, not obeyed
# ---------------------------------------------------------------------------


def test_failed_row_gets_a_bounded_retry(monkeypatch, tmp_path):
    """``failed`` is still unfinished, on purpose — and it is retried.

    A failed merge is not a finished merge: the conflict is still there
    and still needs resolving.  But re-dispatching it *every* round
    under a new id is exactly what turned one stuck conflict into an
    endless stream of them, so the retry is bounded
    (:data:`MAX_BASE_DRIFT_MERGE_ATTEMPTS`).  This plan has had one
    merge task, so it gets exactly one more, and the loop keeps going.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "failed")

    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is True

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert blocked.get("base_drift_repair_task_id")
    assert verification_loop._base_drift_round_action(blocked) == "repair"
    assert len(_base_drift_rows(db_path, PLAN_ID)) == 2


def test_failed_row_makes_persist_write_a_new_task(monkeypatch, tmp_path):
    """The writer answers a failed row with a **new** task, not ``None``.

    ``None`` is what made the round stop forever: the verification loop
    dispatches a repair only when it is handed a task id, so a plan with
    a dead merge task and no new one sat in ``base_drift_conflicts``
    until the end of time (seven plans on 2026-11-12).  The writer now
    writes a new task — a bounded number of times.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "failed")

    orch = _orchestrator(tmp_path)

    task_id = orch._persist_base_drift_repair_task(_report())

    assert task_id is not None
    assert str(task_id).startswith("repair-"), task_id
    assert len(_base_drift_rows(db_path, PLAN_ID)) == 2


# ---------------------------------------------------------------------------
# c) A pending row is handed back — the plan keeps moving
# ---------------------------------------------------------------------------


def test_pending_row_is_handed_back_to_the_executor(monkeypatch, tmp_path):
    """``pending`` is the plainest case: the merge is queued, not done.

    The merge has been written and nobody has finished it — but it *will*
    run.  Writing a second task would give the executor two merges for
    one conflict, and stopping the plan would freeze a conflict somebody
    is already working on.  So the existing id goes back to the loop and
    no second row is written.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "pending")

    assert verification_agent.has_open_base_drift_merge_task(PLAN_ID) is True

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    assert blocked["base_drift_repair_task_id"] == PRIOR_TASK_ID
    assert verification_loop._base_drift_round_action(blocked) == "repair"
    assert len(_base_drift_rows(db_path, PLAN_ID)) == 1


def test_pending_row_makes_persist_return_the_existing_id(monkeypatch, tmp_path):
    """The writer hands the queued task's own id back."""
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "pending")

    orch = _orchestrator(tmp_path)

    task_id = orch._persist_base_drift_repair_task(_report())

    assert task_id == PRIOR_TASK_ID
    assert len(_base_drift_rows(db_path, PLAN_ID)) == 1
