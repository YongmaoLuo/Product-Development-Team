"""``TaskManager.load_tasks`` must NOT hydrate DB-only orphan rows.

2026-09-16
--------------------------
``TaskManager._hydrate_db_only_orphans`` used to run at the end of
``load_tasks``. It was the **second** of two loaders doing the same job:
it called the same ``iter_orphan_tasks`` with the same disk-id set as
``AutonomousAgent._load_tasks`` Phase 2, and Phase 2 overwrites
``self.task_manager.tasks`` wholesale a moment later — so its output was
discarded in every real flow. Its only observable effect was on the
``execution_started`` log counts.

Two copies of the same judgement are what produced the bug:
the two drifts diverged, and a task whose row held a real 895-char
description but no ``test_command`` was downgraded to a content-free
placeholder that the subagent correctly refused to run. The rules now
live once (``orphan_rules``) and are applied once
(``agent._load_tasks`` Phase 2).

These tests pin the removal — the file exists mainly so that a future
"let's add a safety net in TaskManager" change has to argue with a
failing test rather than quietly re-introducing the second copy.

What is still pinned from the old behaviour: the ``save_tasks`` filter
that keeps ``_origin="db_orphan"`` placeholders off disk. Agent Phase 2
still produces those, so that filter is still load-bearing.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from task import SubTask
from task_manager import TaskManager


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    # Set BEFORE constructing TaskManager so any DB read opens the same
    # on-disk DB the test seeded, not the production one.
    os.environ["PDT_STATE_DB_PATH"] = str(db_path)
    connection = open_db(db_path)
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()
        os.environ.pop("PDT_STATE_DB_PATH", None)


def _seed_disk_tasks(tasks_file: Path, task_ids: list[str]) -> None:
    """Write a minimal ``tasks.json`` envelope with the given task ids."""
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    with open(tasks_file, "w") as f:
        json.dump(
            {
                "requirement": "test",
                "tasks": [
                    {
                        "id": tid,
                        "title": f"task {tid}",
                        "description": f"d {tid}",
                        "test_command": "echo ok",
                    }
                    for tid in task_ids
                ],
            },
            f,
        )


def _seed_plan_tasks(
    conn: sqlite3.Connection,
    plan_id: str,
    rows: list[tuple[str, str]],
) -> None:
    """Insert ``plan_tasks`` rows. ``rows`` is ``[(task_id, status), ...]``."""
    for tid, status in rows:
        conn.execute(
            "INSERT INTO plan_tasks (plan_id, task_id, status, updated_at) "
            "VALUES (?, ?, ?, '2026-09-10T00:00:00Z')",
            (plan_id, tid, status),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# The removal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "in_progress"])
def test_non_terminal_db_orphan_is_not_hydrated(
    tmp_path: Path, conn: sqlite3.Connection, status: str,
) -> None:
    """The regression this file exists for.

    A pending DB-only row used to be appended as a placeholder. That is
    now ``agent._load_tasks`` Phase 2's job alone; if this starts passing
    again, the second loader came back.
    """
    plan_id = "test-plan-no-hydrate"
    _seed_plan_tasks(conn, plan_id, [("47", status), ("40-2", status)])
    tasks_file = tmp_path / plan_id / "tasks.json"
    _seed_disk_tasks(tasks_file, ["disk-1", "disk-2"])

    tm = TaskManager(project_dir=tasks_file.parent, tasks_file=tasks_file)

    assert len(tm.tasks) == 2, (
        "TaskManager hydrated DB-only orphans again — that is the "
        "second loader the 2026-09-16 decision removed"
    )
    assert {t.id for t in tm.tasks} == {"disk-1", "disk-2"}
    assert not any(
        getattr(t, "_origin", None) == "db_orphan" for t in tm.tasks
    )


def test_the_method_is_gone():
    """No residue of the removed loader."""
    assert not hasattr(TaskManager, "_hydrate_db_only_orphans")


# ---------------------------------------------------------------------------
# Layout validation (kept — unrelated to the removal)
# ---------------------------------------------------------------------------


def test_reserved_project_dir_name_is_refused(tmp_path: Path) -> None:
    """``<project_dir>/tasks.json`` under a reserved name is rejected.

    Guards against ``plan_id='project'`` colliding across plans in
    ``plan_routing`` (the 2026-09-08 state.db pollution: 22 stale tasks
    rendered into the wrong Feishu card).
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    tasks_file = project_dir / "tasks.json"
    _seed_disk_tasks(tasks_file, ["disk-1"])

    tm = TaskManager(project_dir=project_dir, tasks_file=tasks_file)
    assert tm.plan_id is None


# ---------------------------------------------------------------------------
# Still load-bearing: the save_tasks placeholder filter
# ---------------------------------------------------------------------------


def test_save_tasks_does_not_persist_placeholders(tmp_path: Path) -> None:
    """``_origin="db_orphan"`` tasks must never reach ``tasks.json``.

    ``agent._load_tasks`` Phase 2 still appends placeholders for
    genuinely content-free rows; writing those to disk would make them
    look like authored tasks on the next reload.
    """
    tasks_file = tmp_path / "some-plan" / "tasks.json"
    _seed_disk_tasks(tasks_file, ["disk-1"])

    tm = TaskManager(project_dir=tasks_file.parent, tasks_file=tasks_file)
    placeholder = SubTask(
        id="ghost-1",
        title="[db-orphan:ghost-1]",
        description="recovered from plan_tasks DB (no static fields on disk)",
    )
    placeholder._origin = "db_orphan"  # type: ignore[attr-defined]
    tm.tasks.append(placeholder)

    tm.save_tasks()

    written = json.loads(tasks_file.read_text())["tasks"]
    assert [t["id"] for t in written] == ["disk-1"], (
        "a placeholder was persisted to tasks.json; it will look like a "
        "real authored task on the next load"
    )
