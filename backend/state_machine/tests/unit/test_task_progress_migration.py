"""Unit tests for ``migrate_tasks_json_to_progress``.

Task #3.8 introduces a one-shot migration that lifts per-task runtime
fields from ``tasks.json`` into ``plan_execution.task_progress.tasks``.
The migration must be:

  * Idempotent — running twice does not double-write or change shape
  * Fail-safe — missing plan_execution row, missing file, no runtime
    fields, legacy bare-list, corrupt JSON all skip cleanly
  * Atomic — the SQLite write + file rewrite land as a unit
  * Faithful — ``_repo_version`` survives the migration so post-
    migration writes do not collide with the CAS baseline
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories._task_progress_migration import (
    MIGRATION_SENTINEL,
    RUNTIME_TASK_FIELDS,
    STATIC_TASK_FIELDS,
    migrate_tasks_json_to_progress,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_db(db_path)
    migrate(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plans" / "p1"
    d.mkdir(parents=True)
    return d


def _seed_plan(conn: sqlite3.Connection, plan_id: str = "p1") -> None:
    conn.execute(
        "INSERT INTO plan_execution (plan_id, current_phase, updated_at) "
        "VALUES (?, 'executing', '2026-01-01T00:00:00Z')",
        (plan_id,),
    )


def _write_legacy(tasks_file: Path, payload: dict | list) -> None:
    tasks_file.write_text(json.dumps(payload))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_migrates_status_and_end_ts_into_sqlite(
    conn: sqlite3.Connection, plan_dir: Path,
) -> None:
    """Legacy rows land in ``plan_tasks``; ``tasks.json`` becomes static."""
    _seed_plan(conn)
    tasks_file = plan_dir / "tasks.json"
    _write_legacy(
        tasks_file,
        {
            "requirement": "fix bug",
            "stop_reason": None,
            "reason_detail": None,
            "tasks": [
                {
                    "id": "1",
                    "title": "fix",
                    "description": "d",
                    "test_command": "pytest",
                    "status": "completed",
                    "end_ts": "2026-01-01T00:00:00Z",
                    "_repo_version": 3,
                },
                {
                    "id": "2",
                    "title": "verify",
                    "description": "d",
                    "test_command": "pytest",
                    "status": "in_progress",
                },
            ],
        },
    )

    out = migrate_tasks_json_to_progress("p1", tasks_file, conn)
    assert out is True

    # SQLite side — v4 schema: per-task state lives in plan_tasks.
    # The legacy ``_repo_version`` field is dropped (v4 has no CAS).
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    repo = PlanTaskRepository(conn)
    task1 = repo.get_task("p1", "1")
    task2 = repo.get_task("p1", "2")
    assert task1 is not None
    assert task1["status"] == "completed"
    assert task1["end_ts"] == "2026-01-01T00:00:00Z"
    assert task2 is not None
    assert task2["status"] == "in_progress"
    # No ``end_ts`` on row 2 → entry has only status.
    assert task2.get("end_ts") is None

    # File side — rows are static-only + sentinel.
    after = json.loads(tasks_file.read_text())
    assert after[MIGRATION_SENTINEL] is True
    for row in after["tasks"]:
        assert set(row.keys()) <= STATIC_TASK_FIELDS
    # Envelope preserved.
    assert after["requirement"] == "fix bug"


def test_migration_preserves_runtime_state(
    conn: sqlite3.Connection, plan_dir: Path,
) -> None:
    """Post-migration row carries the legacy ``status`` field.

    Schema v4 normalisation: per-task state lives in ``plan_tasks`` now,
    and ``_repo_version`` is just an audit counter (no more
    application-level CAS).  The
    legacy ``_repo_version`` field in tasks.json is no longer
    meaningful — it is not preserved on migration.  This test pins
    the new contract: post-migration, the row carries ``status``
    (lifted from the legacy tasks.json) and a baseline version.
    """
    from state_machine.repositories.plan_task_repository import PlanTaskRepository

    _seed_plan(conn)
    tasks_file = plan_dir / "tasks.json"
    _write_legacy(
        tasks_file,
        {
            "requirement": "r",
            "tasks": [
                {"id": "1", "title": "t", "description": "d",
                 "status": "completed"},
            ],
        },
    )

    migrate_tasks_json_to_progress("p1", tasks_file, conn)
    repo = PlanTaskRepository(conn)
    task = repo.get_task("p1", "1")
    assert task is not None
    assert task["status"] == "completed"
    # v4 version starts at 1 on insert; legacy _repo_version is not preserved
    assert repo.get_version("p1", "1") >= 1
    # Subsequent write succeeds (no CAS conflict in v4)
    repo.update_task(
        "p1", "1",
        {"status": "failed"},
        expected_version=0,
    )
    assert repo.get_task("p1", "1")["status"] == "failed"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_second_run_is_noop(
    conn: sqlite3.Connection, plan_dir: Path,
) -> None:
    """Second call returns ``False`` and does not rewrite the file."""
    _seed_plan(conn)
    tasks_file = plan_dir / "tasks.json"
    _write_legacy(
        tasks_file,
        {
            "requirement": "r",
            "tasks": [
                {"id": "1", "title": "t", "description": "d",
                 "status": "completed", "_repo_version": 2},
            ],
        },
    )

    first = migrate_tasks_json_to_progress("p1", tasks_file, conn)
    after_first = tasks_file.read_text()
    assert first is True

    second = migrate_tasks_json_to_progress("p1", tasks_file, conn)
    after_second = tasks_file.read_text()
    assert second is False
    # Byte-identical — no rewrite.
    assert after_first == after_second


def test_migration_does_not_clobber_existing_sqlite_entries(
    conn: sqlite3.Connection, plan_dir: Path,
) -> None:
    """An entry already in SQLite wins on collisions (no clobber)."""
    _seed_plan(conn)
    tasks_file = plan_dir / "tasks.json"
    _write_legacy(
        tasks_file,
        {
            "requirement": "r",
            "tasks": [
                {"id": "1", "title": "t", "description": "d",
                 "status": "failed", "_repo_version": 9},
            ],
        },
    )

    # Pre-seed SQLite with a different version.
    conn.execute(
        "UPDATE plan_execution SET task_progress = ? WHERE plan_id = 'p1'",
        ('{"tasks": {"1": {"status": "completed", "_repo_version": 7}}}',),
    )

    migrate_tasks_json_to_progress("p1", tasks_file, conn)
    cur = conn.execute(
        "SELECT task_progress FROM plan_execution WHERE plan_id = 'p1'"
    )
    decoded = json.loads(cur.fetchone()[0])
    # The SQLite entry (status=completed, version=7) survives.
    assert decoded["tasks"]["1"]["status"] == "completed"
    assert decoded["tasks"]["1"]["_repo_version"] == 7


# ---------------------------------------------------------------------------
# Fail-safe paths
# ---------------------------------------------------------------------------


def test_missing_tasks_file_returns_false(
    conn: sqlite3.Connection, plan_dir: Path,
) -> None:
    """No file → no-op."""
    _seed_plan(conn)
    out = migrate_tasks_json_to_progress("p1", plan_dir / "tasks.json", conn)
    assert out is False


def test_missing_plan_execution_row_returns_false(
    conn: sqlite3.Connection, plan_dir: Path,
) -> None:
    """No plan_execution row → no-op (executor has not started yet)."""
    tasks_file = plan_dir / "tasks.json"
    _write_legacy(
        tasks_file,
        {
            "requirement": "r",
            "tasks": [
                {"id": "1", "title": "t", "description": "d",
                 "status": "in_progress"},
            ],
        },
    )
    out = migrate_tasks_json_to_progress("p1", tasks_file, conn)
    assert out is False
    # File untouched.
    after = json.loads(tasks_file.read_text())
    assert "status" in after["tasks"][0]


def test_no_runtime_fields_marks_sentinel_but_skips_sqlite(
    conn: sqlite3.Connection, plan_dir: Path,
) -> None:
    """A static-only legacy file gets the sentinel but no SQLite write."""
    _seed_plan(conn)
    tasks_file = plan_dir / "tasks.json"
    _write_legacy(
        tasks_file,
        {
            "requirement": "r",
            "tasks": [
                {"id": "1", "title": "t", "description": "d",
                 "test_command": "pytest", "depends_on": []},
            ],
        },
    )

    out = migrate_tasks_json_to_progress("p1", tasks_file, conn)
    assert out is True
    after = json.loads(tasks_file.read_text())
    assert after[MIGRATION_SENTINEL] is True
    # SQLite column untouched (still empty).
    cur = conn.execute(
        "SELECT task_progress FROM plan_execution WHERE plan_id = 'p1'"
    )
    assert cur.fetchone()[0] in (None, "")


def test_legacy_bare_list_is_wrapped_in_envelope(
    conn: sqlite3.Connection, plan_dir: Path,
) -> None:
    """A bare-list legacy file (no envelope) is normalised into envelope form."""
    _seed_plan(conn)
    tasks_file = plan_dir / "tasks.json"
    _write_legacy(
        tasks_file,
        [
            {"id": "1", "title": "t", "description": "d",
             "status": "completed", "_repo_version": 1},
        ],
    )

    out = migrate_tasks_json_to_progress("p1", tasks_file, conn)
    assert out is True
    after = json.loads(tasks_file.read_text())
    assert isinstance(after, dict)
    assert "tasks" in after
    assert after[MIGRATION_SENTINEL] is True
    assert after["requirement"] == ""


def test_corrupt_json_returns_false(
    conn: sqlite3.Connection, plan_dir: Path,
) -> None:
    """A file with invalid JSON returns ``False`` without raising."""
    _seed_plan(conn)
    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text("{not-valid-json")
    out = migrate_tasks_json_to_progress("p1", tasks_file, conn)
    assert out is False


def test_already_migrated_returns_false(
    conn: sqlite3.Connection, plan_dir: Path,
) -> None:
    """A file with the sentinel set returns ``False`` (no rewrite)."""
    _seed_plan(conn)
    tasks_file = plan_dir / "tasks.json"
    _write_legacy(
        tasks_file,
        {
            MIGRATION_SENTINEL: True,
            "requirement": "r",
            "tasks": [{"id": "1", "title": "t", "description": "d"}],
        },
    )
    out = migrate_tasks_json_to_progress("p1", tasks_file, conn)
    assert out is False
    # Unchanged.
    after = json.loads(tasks_file.read_text())
    assert after[MIGRATION_SENTINEL] is True


# ---------------------------------------------------------------------------
# Coverage of the runtime-field set
# ---------------------------------------------------------------------------


def test_all_runtime_fields_are_lifted(
    conn: sqlite3.Connection, plan_dir: Path,
) -> None:
    """Every field in :data:`RUNTIME_TASK_FIELDS` makes it into SQLite.

    Schema v4: the legacy ``_repo_version`` field is dropped (v4 has no
    application-level CAS).  All other runtime fields land in
    ``plan_tasks``.
    """
    _seed_plan(conn)
    tasks_file = plan_dir / "tasks.json"
    runtime_row = {
        "id": "1",
        "title": "t",
        "description": "d",
        "status": "completed",
        "end_ts": "2026-01-01T00:00:00Z",
        "schedule_ts": "2025-12-31T23:59:00Z",
        "attempt": 3,
        "commit_sha": "abc123",
        "failure_reason": "boom",
        "breakdown_count": 2,
        "_repo_version": 7,
    }
    _write_legacy(
        tasks_file,
        {"requirement": "r", "tasks": [runtime_row]},
    )

    migrate_tasks_json_to_progress("p1", tasks_file, conn)
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    repo = PlanTaskRepository(conn)
    task = repo.get_task("p1", "1")
    assert task is not None
    # Every runtime field except ``_repo_version`` is lifted
    # (``_repo_version`` is dropped in v4).
    for field in RUNTIME_TASK_FIELDS:
        if field == "_repo_version":
            continue
        if field in runtime_row:
            assert task.get(field) == runtime_row[field], (
                f"runtime field {field!r} not lifted correctly"
            )
    # The file row is static-only.
    after = json.loads(tasks_file.read_text())
    file_row = after["tasks"][0]
    for field in RUNTIME_TASK_FIELDS:
        assert field not in file_row
    assert set(file_row.keys()) <= STATIC_TASK_FIELDS


def test_partial_runtime_entries_only_carry_what_was_set(
    conn: sqlite3.Connection, plan_dir: Path,
) -> None:
    """A row with only ``status`` set lifts only ``status``.

    Schema v4: ``_repo_version`` is no longer lifted (v4 has no CAS).
    Only the runtime fields that were actually set on the source row are
    persisted.
    """
    _seed_plan(conn)
    tasks_file = plan_dir / "tasks.json"
    _write_legacy(
        tasks_file,
        {
            "requirement": "r",
            "tasks": [
                {"id": "1", "title": "t", "description": "d",
                 "status": "in_progress"},
            ],
        },
    )

    migrate_tasks_json_to_progress("p1", tasks_file, conn)
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    repo = PlanTaskRepository(conn)
    task = repo.get_task("p1", "1")
    assert task is not None
    assert task["status"] == "in_progress"
    # Only fields with non-None values were actually lifted. (v4
    # ``get_task`` returns all columns with None for unset ones —
    # we filter to verify only status was lifted from the legacy
    # row.)
    runtime_keys = {
        "status", "end_ts", "schedule_ts", "attempt", "commit_sha",
        "failure_reason", "breakdown_count",
    }
    lifted_runtime = {
        k: v for k, v in task.items()
        if k in runtime_keys and v is not None
    }
    assert lifted_runtime == {"status": "in_progress"}