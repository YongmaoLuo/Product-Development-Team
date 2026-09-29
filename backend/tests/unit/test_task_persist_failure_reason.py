"""``failure_reason`` must reach the ``plan_tasks`` SQLite overlay.

Background: tasks failed with readable, specific reasons in
``execution.log``
(cross-verify mismatch, empty diff, orphan task, missing test report)
but the progress API — which overlays runtime fields from
``plan_tasks`` (``server.py`` ``_RUNTIME_OVERLAY_FIELDS``) — rendered
``failure_reason: null`` for all of them, so the task card showed
"未知原因".

Root cause: ``TaskManager.record_task_failure`` set the reason on the
in-memory ``SubTask`` and in ``runtime_overrides``, then called
``_persist_status_to_sqlite`` — which persisted only
``{"status", "end_ts"}``. The executor subprocess exits after the run,
so the in-memory copies died with the process and the durable overlay
row stayed NULL forever.

Contract pinned here:

  1. ``record_task_failure`` writes ``failure_reason`` into
     ``plan_tasks`` (not just memory).
  2. A non-failed persist (e.g. ``completed``) does not invent a
     ``failure_reason`` column value.
  3. A failed persist with no recorded reason still succeeds — the
     row must carry ``status='failed'`` and a NULL reason rather than
     crash or fabricate text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate


def _make_tm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plan_dir: str):
    """A real TaskManager pointed at a tmp state.db via PDT_STATE_DB_PATH."""
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))

    from task import SubTask
    from task_manager import TaskManager

    tasks_file = tmp_path / plan_dir / "tasks.json"
    tasks_file.parent.mkdir()
    tasks_file.write_text("[]")
    tm = TaskManager(project_dir=tasks_file.parent, tasks_file=tasks_file)
    tm.tasks = [
        SubTask(
            id="t-1",
            title="example",
            description="example description",
            test_command="pytest -q",
            files_to_modify=["foo.py"],
        )
    ]
    return tm, db_path


def _read_row(db_path: Path, plan_id: str, task_id: str):
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT status, failure_reason FROM plan_tasks "
            "WHERE plan_id = ? AND task_id = ?",
            (plan_id, task_id),
        )
        return cur.fetchone()
    finally:
        conn.close()


def test_record_task_failure_persists_reason_to_plan_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The earlier-plan regression: reason must survive the subprocess exit."""
    tm, db_path = _make_tm(tmp_path, monkeypatch, "persist-reason-plan")
    from task_manager import derive_plan_id_from_tasks_file

    plan_id = derive_plan_id_from_tasks_file(tm.tasks_file)
    tm.record_task_failure("t-1", "empty_diff_no_changes: nothing modified")

    row = _read_row(db_path, plan_id, "t-1")
    assert row is not None, "plan_tasks row must exist after persist"
    assert row[0] == "failed"
    assert row[1] == "empty_diff_no_changes: nothing modified", (
        "failure_reason was dropped at the persist boundary — the "
        "progress API overlay reads this column, so a NULL here is "
        "exactly what rendered '未知原因' on the task card"
    )


def test_completed_persist_leaves_failure_reason_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tm, db_path = _make_tm(tmp_path, monkeypatch, "persist-clean-plan")
    from task_manager import derive_plan_id_from_tasks_file

    plan_id = derive_plan_id_from_tasks_file(tm.tasks_file)
    tm.update_task_status("t-1", "completed")

    row = _read_row(db_path, plan_id, "t-1")
    assert row is not None
    assert row[0] == "completed"
    assert row[1] is None, (
        "a completed task must not carry a fabricated failure_reason"
    )


def test_failed_persist_without_reason_still_writes_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No reason recorded (unexpected failure path) → status lands,
    reason stays NULL — never crash, never invent text."""
    tm, db_path = _make_tm(tmp_path, monkeypatch, "persist-bare-plan")
    from task_manager import derive_plan_id_from_tasks_file

    plan_id = derive_plan_id_from_tasks_file(tm.tasks_file)
    # Direct persist call with no prior record_task_failure.
    tm._persist_status_to_sqlite("t-1", "failed")

    row = _read_row(db_path, plan_id, "t-1")
    assert row is not None
    assert row[0] == "failed"
    assert row[1] is None
