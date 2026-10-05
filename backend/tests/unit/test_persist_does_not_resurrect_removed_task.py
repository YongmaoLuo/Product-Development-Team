"""A task the plan has dropped must not come back through a status write.

2026-09-17. ``PlanTaskRepository.update_task`` is an ``INSERT ... ON
CONFLICT DO UPDATE`` — an upsert. So writing *runtime* state for a task
whose row was just deleted re-creates the row, and re-creates it
content-free, because the payload carries runtime fields only.

That is how a refiner split left a phantom ledger entry:

    refine_structure_applied   ← deleted repair-r3-03-1
                                              from both stores
    task_failed                ← put the row back

The resurrected row had ``title`` / ``description`` / ``test_command``
all NULL — only ``status``, ``end_ts``, ``_repo_version`` and
``updated_at`` were set. It is inert for scheduling (the next
``_load_tasks`` Phase 2 sees a terminal orphan and supersedes it), but
the operator's instruction was that a split parent is *deleted*, and it
was — for 29 milliseconds.

The two ``TaskManager`` mirrors already refused to record a task the plan
no longer contains; only the ``runtime_overrides`` one was wired to that
condition. These tests pin that the SQLite mirror obeys it too, and that
the guard does not over-block the legitimate case.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent import AutonomousAgent  # noqa: E402
from task import SubTask  # noqa: E402
from task_manager import TaskManager  # noqa: E402

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)


#: The tasks file has to live at ``<tmp>/<PLAN_ID>/tasks.json``:
#: ``TaskManager._persist_status_to_sqlite`` derives the plan id from
#: ``derive_plan_id_from_tasks_file(self.tasks_file)``, so a random
#: ``tmp_path`` name would make it write under a different plan and the
#: test would pass vacuously.
PLAN_ID = "test-plan-no-resurrect"


def _task(tid: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": tid,
        "title": f"task {tid}",
        "description": "d",
        "test_command": "pytest -q",
        "files_to_modify": ["__NO_FILE_CHANGES__"],
        "depends_on": [],
        "model_type": "medium",
        "project_dir": None,
    }
    out.update(extra)
    return out


class _StubAgent:
    """Host for ``AutonomousAgent._apply_refiner_structure`` / ``_persist_task_status``."""

    _TERMINAL_REPAIR_TASK_GROUP_PREFIX = (
        AutonomousAgent._TERMINAL_REPAIR_TASK_GROUP_PREFIX
    )
    _apply_refiner_structure = AutonomousAgent._apply_refiner_structure
    _is_refiner_protected = AutonomousAgent._is_refiner_protected
    _persist_task_status = AutonomousAgent._persist_task_status

    def __init__(self, task_manager, repo, logger=None, plan_id=PLAN_ID):
        self.task_manager = task_manager
        self._repo = repo
        self.plan_id = plan_id
        self.logger = logger

    def _get_task_progress_repository(self):
        return self._repo


class _LogRecorder:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def _record(self, level):
        def _fn(event, message, **kwargs):
            self.events.append({
                "event": event, "message": message, "data": kwargs.get("data"),
            })
        return _fn

    def __getattr__(self, name):
        if name in ("info", "warning", "error", "debug"):
            return self._record(name)
        raise AttributeError(name)

    def events_named(self, event: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    plan_dir = tmp_path / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(plan_dir / "state.db"))
    c = open_db(plan_dir / "state.db")
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _harness(tmp_path: Path, disk_tasks, repo, tasks_file=None):
    plan_dir = tmp_path / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks_file = tasks_file or plan_dir / "tasks.json"
    tasks_file.write_text(
        json.dumps({"tasks": disk_tasks}, ensure_ascii=False), encoding="utf-8",
    )
    tm = TaskManager(project_dir=plan_dir, tasks_file=tasks_file)
    return _StubAgent(tm, repo), tm, tasks_file


def _read_tasks_file(tasks_file: Path) -> List[Dict[str, Any]]:
    return json.loads(tasks_file.read_text(encoding="utf-8"))["tasks"]


# ---------------------------------------------------------------------------
# The guard on the two TaskManager mirrors
# ---------------------------------------------------------------------------


def test_record_task_failure_does_not_resurrect_a_removed_task(tmp_path, conn):
    agent, tm, _ = _harness(tmp_path, [_task("1")], PlanTaskRepository(conn))
    repo = PlanTaskRepository(conn)
    repo.delete_task(PLAN_ID, "gone")  # idempotent; nothing there yet

    tm.record_task_failure("gone", "boom")

    assert "gone" not in repo.load_all(PLAN_ID), (
        "record_task_failure re-created a row for a task the plan no "
        "longer contains — the upsert in update_task resurrects it"
    )


def test_update_task_status_does_not_resurrect_a_removed_task(tmp_path, conn):
    agent, tm, _ = _harness(tmp_path, [_task("1")], PlanTaskRepository(conn))
    repo = PlanTaskRepository(conn)

    tm.update_task_status("gone", "completed")

    assert "gone" not in repo.load_all(PLAN_ID)


def test_update_task_commit_sha_does_not_resurrect_a_removed_task(tmp_path, conn):
    """The third writer needs the same guard as the other two.

    ``update_task_commit_sha`` had a membership check, but it wrapped
    only the ``runtime_overrides`` mirror — the SQLite write sat outside
    it, so the guard was present in the source and absent in effect.
    A refiner split that deleted a parent therefore left behind a
    content-free row (status / title / description all NULL, commit_sha
    set), and the dispatcher reported "No schedulable micro-layer
    found" while the real children were still pending.
    """
    agent, tm, _ = _harness(tmp_path, [_task("1")], PlanTaskRepository(conn))
    repo = PlanTaskRepository(conn)

    tm.update_task_commit_sha("gone", "a" * 40)

    assert "gone" not in repo.load_all(PLAN_ID), (
        "update_task_commit_sha re-created a row for a task the plan no "
        "longer contains — its guard wrapped runtime_overrides only, "
        "leaving the SQLite upsert unguarded"
    )


def test_the_commit_sha_guard_does_not_over_block_a_live_task(tmp_path, conn):
    """A task the plan still contains must still record its commit."""
    agent, tm, _ = _harness(tmp_path, [_task("1")], PlanTaskRepository(conn))
    repo = PlanTaskRepository(conn)
    sha = "b" * 40

    tm.update_task_commit_sha("1", sha)

    rows = repo.load_all(PLAN_ID)
    assert "1" in rows, "the guard must not block a task the plan contains"
    assert (rows["1"].get("commit_sha") if isinstance(rows["1"], dict) else rows["1"]) == sha


def test_the_guard_does_not_over_block_a_live_task(tmp_path, conn):
    """A task the plan still contains must keep persisting normally."""
    agent, tm, _ = _harness(
        tmp_path, [_task("1"), _task("2")], PlanTaskRepository(conn),
    )
    repo = PlanTaskRepository(conn)

    tm.record_task_failure("1", "boom")
    tm.update_task_status("2", "completed")

    rows = repo.load_all(PLAN_ID)
    assert rows["1"]["status"] == "failed"
    assert rows["1"]["failure_reason"] == "boom"
    assert rows["2"]["status"] == "completed"


def test_agent_persist_skips_a_task_outside_the_plan(tmp_path, conn):
    repo = PlanTaskRepository(conn)
    agent, tm, _ = _harness(tmp_path, [_task("1")], repo)
    agent.logger = _LogRecorder()

    agent._persist_task_status(
        SubTask(id="gone", title="t", description="d", status="failed"),
    )

    assert "gone" not in repo.load_all(PLAN_ID)
    assert agent.logger.events_named("task_persist_skipped_removed")


def test_agent_persist_still_writes_a_live_task(tmp_path, conn):
    repo = PlanTaskRepository(conn)
    agent, tm, _ = _harness(tmp_path, [_task("1")], repo)

    agent._persist_task_status(
        SubTask(id="1", title="t", description="d", status="failed"),
    )

    assert repo.load_all(PLAN_ID)["1"]["status"] == "failed"


# ---------------------------------------------------------------------------
# End to end: the exact failing sequence
# ---------------------------------------------------------------------------


def test_a_refiner_split_leaves_no_phantom_row(tmp_path, conn):
    """split parent → record its failure → the row must stay gone.

    This is the observed sequence, in
    order: the refinement deletes the parent from both stores, and then
    the executor's failure recording runs for the task it was working on
    when the split happened.
    """
    repo = PlanTaskRepository(conn)
    for t in (_task("1"), _task("40")):
        repo.add_task(PLAN_ID, t)

    agent, tm, tasks_file = _harness(tmp_path, [_task("1"), _task("40")], repo)
    current = [t.model_dump() for t in tm.tasks]

    applied = agent._apply_refiner_structure(
        current, [_task("1"), _task("40-1")],
        SubTask(id="40", title="t", description="d"),
    )
    assert applied is True
    assert "40" not in repo.load_all(PLAN_ID)

    # The executor is still holding the pre-split ``SubTask`` and now
    # records its failure.
    tm.record_task_failure("40", "pytest exit 1")
    agent._persist_task_status(
        SubTask(id="40", title="t", description="d", status="failed"),
    )

    rows = repo.load_all(PLAN_ID)
    assert "40" not in rows, (
        "the phantom ledger row is back — a split parent must stay deleted"
    )
    assert "40-1" in rows
    assert [t["id"] for t in _read_tasks_file(tasks_file)] == ["1", "40-1"]
