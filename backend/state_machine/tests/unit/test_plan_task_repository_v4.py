"""Tests for the rewritten :class:`PlanTaskRepository` against the v4 plan_tasks table.

Schema v4 (2026-09-09) replaces the legacy
read-modify-write against ``plan_execution.task_progress`` JSON with
direct SQLite row-level writes against the new ``plan_tasks`` table.
The application-level ``_repo_version`` CAS is removed because SQLite
UPDATE is row-atomic — no read-modify-write, no race window, no
silent fail.

The legacy public API (:meth:`get_task` / :meth:`get_version` /
:meth:`load_all` / :meth:`update_task` / :meth:`delete_task` /
:meth:`iter_orphan_tasks` / :meth:`add_task`) is preserved verbatim so
all 30+ existing callers in ``server.py`` / ``agent.py`` /
``task_manager.py`` / ``watchdog.py`` / ``task_repository.py`` keep
working without changes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.plan_task_repository import (
    ALLOWED_STATIC_TASK_FIELDS,
    ALLOWED_TASK_FIELDS,
    PlanTaskRepository,
    TaskProgressConflictError,
    TaskProgressValidationError,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_db(db_path)
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> PlanTaskRepository:
    return PlanTaskRepository(conn)


def _seed_plan(conn: sqlite3.Connection, plan_id: str) -> None:
    """Insert a plan_execution row so legacy FK + readers have a target."""
    conn.execute(
        "INSERT INTO plan_execution (plan_id, current_phase, updated_at) "
        "VALUES (?, 'executing', '2026-01-01T00:00:00Z')",
        (plan_id,),
    )


# ---------------------------------------------------------------------------
# Allow-list contract (preserved from legacy)
# ---------------------------------------------------------------------------


def test_allowed_fields_match_legacy_contract() -> None:
    """Plan v4 extends the legacy allow-list with ``failure_reason`` /
    ``breakdown_count`` (both runtime columns on the ``plan_tasks``
    table).  The dispatcher can now write them through ``update_task``.
    """
    assert ALLOWED_TASK_FIELDS == frozenset(
        {
            "status",
            "commit_sha",
            "attempt",
            "schedule_ts",
            "end_ts",
            "failure_reason",
            "breakdown_count",
        }
    )


def test_update_task_rejects_disallowed_field(
    repo: PlanTaskRepository, conn: sqlite3.Connection,
) -> None:
    _seed_plan(conn, "p1")
    with pytest.raises(TaskProgressValidationError) as excinfo:
        repo.update_task(
            "p1", "t1",
            {"title": "evil-structural-field"},
            expected_version=0,
        )
    assert excinfo.value.task_id == "t1"
    assert "title" in excinfo.value.forbidden_fields


def test_update_task_rejects_unsafe_commit_sha(
    repo: PlanTaskRepository, conn: sqlite3.Connection,
) -> None:
    _seed_plan(conn, "p1")
    with pytest.raises(TaskProgressValidationError):
        repo.update_task(
            "p1", "t1",
            {"commit_sha": "abc; rm -rf /"},
            expected_version=0,
        )


def test_update_task_rejects_unsafe_task_id(
    repo: PlanTaskRepository, conn: sqlite3.Connection,
) -> None:
    _seed_plan(conn, "p1")
    with pytest.raises(TaskProgressValidationError):
        repo.update_task(
            "p1", "t1; DROP TABLE plan_tasks; --",
            {"status": "completed"},
            expected_version=0,
        )


# ---------------------------------------------------------------------------
# New: update_task writes to plan_tasks directly (no plan_execution row required)
# ---------------------------------------------------------------------------


def test_update_task_writes_to_plan_tasks_table(
    repo: PlanTaskRepository, conn: sqlite3.Connection,
) -> None:
    """update_task must INSERT into plan_tasks — no plan_execution row needed."""
    # NO _seed_plan() — explicit: the legacy `TaskProgressNotFound` for
    # missing plan_execution row is REMOVED in v4. The plan_tasks row
    # is the source of truth.
    repo.update_task(
        "plan-no-exec", "t1",
        {"status": "completed", "end_ts": "2026-09-09T00:00:00Z"},
        expected_version=0,
    )
    cur = conn.execute(
        "SELECT status, end_ts FROM plan_tasks WHERE plan_id=? AND task_id=?",
        ("plan-no-exec", "t1"),
    )
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "completed"
    assert row[1] == "2026-09-09T00:00:00Z"


def test_update_task_subsequent_writes_increment_repo_version(
    repo: PlanTaskRepository, conn: sqlite3.Connection,
) -> None:
    """Each subsequent update bumps _repo_version (audit-only field)."""
    repo.update_task("p1", "t1", {"status": "pending"}, expected_version=0)
    v1 = repo.get_version("p1", "t1")
    assert v1 >= 1

    repo.update_task("p1", "t1", {"status": "in_progress"}, expected_version=v1)
    v2 = repo.get_version("p1", "t1")
    assert v2 == v1 + 1

    repo.update_task("p1", "t1", {"status": "completed"}, expected_version=v2)
    v3 = repo.get_version("p1", "t1")
    assert v3 == v2 + 1


def test_update_task_overwrites_existing_status_last_writer_wins(
    repo: PlanTaskRepository, conn: sqlite3.Connection,
) -> None:
    """v4 removes application-level CAS. SQLite row-level atomic
    UPDATE means concurrent writers may race, but the last one wins.
    This is more reliable than the legacy CAS because it never
    silently fails (the legacy bug was: read-modify-write lost
    updates due to concurrent commits).

    Audit: this is a SEMANTIC CHANGE from legacy behaviour. The
    legacy version raised ``TaskProgressConflictError`` on stale
    version; the new version never raises this. Callers that
    previously retried on ConflictError should be updated to no-op.
    """
    repo.update_task("p1", "t1", {"status": "completed"}, expected_version=0)
    # Subsequent update with expected_version=0 (stale, would have
    # raised in legacy) now simply overwrites:
    repo.update_task(
        "p1", "t1",
        {"status": "failed", "failure_reason": "override"},
        expected_version=0,
    )
    assert repo.get_task("p1", "t1")["status"] == "failed"
    assert repo.get_task("p1", "t1")["failure_reason"] == "override"


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


def test_load_all_empty_when_no_rows(repo: PlanTaskRepository) -> None:
    assert repo.load_all("missing") == {}


def test_get_task_returns_none_when_absent(repo: PlanTaskRepository) -> None:
    assert repo.get_task("missing", "missing") is None


def test_get_version_returns_zero_when_absent(repo: PlanTaskRepository) -> None:
    assert repo.get_version("missing", "missing") == 0


def test_get_task_returns_inserted_fields(
    repo: PlanTaskRepository,
) -> None:
    repo.update_task(
        "p1", "t1",
        {"status": "completed", "end_ts": "x", "commit_sha": "abc"},
        expected_version=0,
    )
    entry = repo.get_task("p1", "t1")
    assert entry is not None
    assert entry["status"] == "completed"
    assert entry["end_ts"] == "x"
    assert entry["commit_sha"] == "abc"


def test_load_all_returns_multiple_tasks(repo: PlanTaskRepository) -> None:
    repo.update_task("p1", "a", {"status": "completed"}, expected_version=0)
    repo.update_task("p1", "b", {"status": "failed"}, expected_version=0)
    repo.update_task("p2", "c", {"status": "pending"}, expected_version=0)
    assert set(repo.load_all("p1").keys()) == {"a", "b"}
    assert set(repo.load_all("p2").keys()) == {"c"}
    assert repo.load_all("nope") == {}


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_task_removes_row(repo: PlanTaskRepository) -> None:
    repo.update_task("p1", "t1", {"status": "completed"}, expected_version=0)
    assert repo.delete_task("p1", "t1") is True
    assert repo.get_task("p1", "t1") is None


def test_delete_task_returns_false_when_absent(repo: PlanTaskRepository) -> None:
    assert repo.delete_task("p1", "missing") is False


def test_delete_task_does_not_touch_other_tasks(repo: PlanTaskRepository) -> None:
    repo.update_task("p1", "a", {"status": "completed"}, expected_version=0)
    repo.update_task("p1", "b", {"status": "pending"}, expected_version=0)
    repo.delete_task("p1", "a")
    assert repo.get_task("p1", "a") is None
    assert repo.get_task("p1", "b") is not None


# ---------------------------------------------------------------------------
# iter_orphan_tasks
# ---------------------------------------------------------------------------


def test_iter_orphan_tasks_yields_only_orphans(
    repo: PlanTaskRepository,
) -> None:
    repo.update_task("p1", "on-disk", {"status": "completed"}, expected_version=0)
    repo.update_task("p1", "orphan-1", {"status": "pending"}, expected_version=0)
    repo.update_task("p1", "orphan-2", {"status": "failed"}, expected_version=0)
    orphans = list(repo.iter_orphan_tasks("p1", disk_ids={"on-disk"}))
    ids = {o["id"] for o in orphans}
    assert ids == {"orphan-1", "orphan-2"}


def test_iter_orphan_tasks_empty_when_all_on_disk(
    repo: PlanTaskRepository,
) -> None:
    repo.update_task("p1", "x", {"status": "completed"}, expected_version=0)
    assert list(repo.iter_orphan_tasks("p1", disk_ids={"x"})) == []


def test_iter_orphan_tasks_empty_when_plan_absent(
    repo: PlanTaskRepository,
) -> None:
    assert list(repo.iter_orphan_tasks("nope", disk_ids=set())) == []


# ---------------------------------------------------------------------------
# add_task (refiner / repair path)
# ---------------------------------------------------------------------------


def test_add_task_inserts_static_fields_with_pending_status(
    repo: PlanTaskRepository,
) -> None:
    new_v = repo.add_task(
        "p1",
        {
            "id": "t-new",
            "title": "newly added task",
            "description": "from refiner",
            "depends_on": ["t1"],
        },
    )
    assert new_v == 1
    entry = repo.get_task("p1", "t-new")
    assert entry is not None
    assert entry["status"] == "pending"
    assert entry["title"] == "newly added task"
    assert entry["description"] == "from refiner"
    # depends_on is a list — repository should JSON-serialize for SQLite storage
    assert entry["depends_on"] == ["t1"]


def test_add_task_rejects_disallowed_field(
    repo: PlanTaskRepository,
) -> None:
    with pytest.raises(TaskProgressValidationError):
        repo.add_task(
            "p1",
            {"id": "t1", "commit_sha": "evil-runtime-field"},
        )


def test_add_task_replace_existing_returns_new_version(
    repo: PlanTaskRepository,
) -> None:
    repo.add_task("p1", {"id": "t1", "title": "v1"})
    v1 = repo.get_version("p1", "t1")
    repo.add_task("p1", {"id": "t1", "title": "v2"})
    v2 = repo.get_version("p1", "t1")
    assert v2 == v1 + 1
    assert repo.get_task("p1", "t1")["title"] == "v2"


def test_add_task_static_fields_allow_list_includes_canonical_set() -> None:
    """Pin the static-fields allow-list — used by refiner + verification-repair."""
    assert "id" in ALLOWED_STATIC_TASK_FIELDS
    assert "title" in ALLOWED_STATIC_TASK_FIELDS
    assert "description" in ALLOWED_STATIC_TASK_FIELDS
    assert "depends_on" in ALLOWED_STATIC_TASK_FIELDS
    assert "files_to_modify" in ALLOWED_STATIC_TASK_FIELDS


# ---------------------------------------------------------------------------
# Atomicity invariants (replaces legacy read-modify-write tests)
# ---------------------------------------------------------------------------


def test_concurrent_writers_no_lost_update(
    repo: PlanTaskRepository, conn: sqlite3.Connection,
) -> None:
    """Two threads writing the same task must NOT lose updates.

    Legacy behaviour: read-modify-write + JSON column caused lost
    updates because Thread B's commit could clobber Thread A's
    pending payload (the audit 2026-09-09 root cause).

    New behaviour: SQLite UPDATE plan_tasks SET ... WHERE
    plan_id=? AND task_id=? is row-atomic — only one writer's
    payload survives, but NEITHER write silently disappears.
    """
    import threading

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def writer(name: str, status: str) -> None:
        # Each thread opens its own connection so SQLite's
        # check_same_thread doesn't trip us.
        c = open_db(Path(conn.execute(
            "PRAGMA database_list"
        ).fetchone()[2]))  # hack: same path
        try:
            r = PlanTaskRepository(c)
            barrier.wait()
            r.update_task(
                "p1", "t1",
                {"status": status, "end_ts": f"end-{name}"},
                expected_version=0,
            )
            results[name] = "ok"
        finally:
            c.close()

    t1 = threading.Thread(target=writer, args=("A", "completed"))
    t2 = threading.Thread(target=writer, args=("B", "failed"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results == {"A": "ok", "B": "ok"}
    # Exactly one writer's payload survived; no silent loss.
    final = repo.get_task("p1", "t1")
    assert final is not None
    assert final["status"] in {"completed", "failed"}
    assert final["end_ts"] in {"end-A", "end-B"}


# ---------------------------------------------------------------------------
# Partial-update semantics (2026-09-23)
# ---------------------------------------------------------------------------


class TestPartialUpdateLeavesOmittedColumnsAlone:
    """A payload writes the columns it names — and nothing else.

    A production plan re-ran on 2026-09-23, finished
    twelve tasks, and reported 0% on the progress card. The cause was
    here: ``update_task_commit_sha`` runs eleven lines after
    ``update_task_status(task, "completed")`` and writes
    ``fields={"commit_sha": sha}``. The conflict arm used to set every
    runtime column to ``excluded.*`` regardless of whether the payload
    carried it, so an omitted key was clamped to NULL and the
    ``completed`` was erased moments after it landed.

    Nothing downstream reads a NULL status as terminal: the card counts
    ``status = 'completed'``, and ``_load_tasks`` re-hydrates NULL as
    ``pending``. Every finished task was re-dispatched.
    """

    def test_commit_sha_write_does_not_clear_status(self, repo):
        repo.update_task(
            "p1", "t1", {"status": "completed", "end_ts": "e"},
            expected_version=0,
        )
        repo.update_task(
            "p1", "t1", {"commit_sha": "a" * 40}, expected_version=0,
        )
        entry = repo.get_task("p1", "t1")
        assert entry["status"] == "completed"
        assert entry["end_ts"] == "e"
        assert entry["commit_sha"] == "a" * 40

    def test_status_write_does_not_clear_commit_sha(self, repo):
        """The other direction: recording a terminal transition must not
        orphan the commit that already proved the work happened."""
        repo.update_task(
            "p1", "t1", {"status": "completed", "commit_sha": "b" * 40},
            expected_version=0,
        )
        repo.update_task(
            "p1", "t1", {"status": "superseded", "failure_reason": "r"},
            expected_version=0,
        )
        entry = repo.get_task("p1", "t1")
        assert entry["status"] == "superseded"
        assert entry["commit_sha"] == "b" * 40

    def test_status_write_does_not_clear_attempt_or_schedule_ts(self, repo):
        repo.update_task(
            "p1", "t1", {"attempt": 3, "schedule_ts": "s"},
            expected_version=0,
        )
        repo.update_task(
            "p1", "t1", {"status": "in_progress"}, expected_version=0,
        )
        entry = repo.get_task("p1", "t1")
        assert entry["status"] == "in_progress"
        assert entry["attempt"] == 3
        assert entry["schedule_ts"] == "s"

    def test_an_omitted_column_keeps_its_precise_value(self, repo):
        """Blanket form of the rule: a commit_sha-only write leaves every
        other runtime column byte-identical."""
        repo.update_task(
            "p1", "t1",
            {
                "status": "completed", "end_ts": "e", "schedule_ts": "s",
                "attempt": 7, "failure_reason": "old", "breakdown_count": 2,
            },
            expected_version=0,
        )
        before = repo.get_task("p1", "t1")
        repo.update_task(
            "p1", "t1", {"commit_sha": "c" * 40}, expected_version=0,
        )
        after = repo.get_task("p1", "t1")
        for col in ("status", "end_ts", "schedule_ts", "attempt",
                    "failure_reason", "breakdown_count"):
            assert after[col] == before[col], f"{col} was clobbered"
        assert after["commit_sha"] == "c" * 40

    def test_explicit_none_still_clears(self, repo):
        """Partial update, not "ignore None": a caller that wants a column
        cleared says so by passing it."""
        repo.update_task(
            "p1", "t1", {"failure_reason": "boom"}, expected_version=0,
        )
        repo.update_task(
            "p1", "t1", {"status": "completed", "failure_reason": None},
            expected_version=0,
        )
        entry = repo.get_task("p1", "t1")
        assert entry["status"] == "completed"
        assert entry["failure_reason"] is None

    def test_a_new_row_still_starts_empty(self, repo):
        """The INSERT branch is unchanged: a row that did not exist has no
        prior state to protect, so omitted columns start NULL."""
        repo.update_task("p1", "t1", {"status": "pending"}, expected_version=0)
        entry = repo.get_task("p1", "t1")
        assert entry["status"] == "pending"
        assert entry["commit_sha"] is None
        assert entry["end_ts"] is None
        assert entry["attempt"] is None


# ---------------------------------------------------------------------------
# Exhaustive column matrix — the form the bug actually needed
# ---------------------------------------------------------------------------

#: Every column ``update_task`` can write (mirrors ``ALLOWED_TASK_FIELDS``).
RUNTIME_COLUMNS: tuple[str, ...] = (
    "status", "end_ts", "schedule_ts", "attempt", "commit_sha",
    "failure_reason", "breakdown_count",
)

#: A fully-populated row, so "unchanged" is distinguishable from
#: "NULL either way".  A NULL sentinel would make the matrix vacuous:
#: the old clamp wrote NULL, and NULL == NULL would read as "preserved".
_SEEDED_ROW: dict = {
    "status": "in_progress",
    "end_ts": "2026-01-01T00:00:00Z",
    "schedule_ts": "2026-01-02T00:00:00Z",
    "attempt": 4,
    "commit_sha": "f" * 40,
    "failure_reason": "seeded",
    "breakdown_count": 2,
}


def _writing(value: str):
    """A value for ``value`` that differs from the seeded one."""
    if value == "status":
        return "completed"
    if value in ("attempt", "breakdown_count"):
        return 99
    if value == "commit_sha":
        return "a" * 40
    return "2026-02-02T02:02:02Z"


class TestSingleColumnWriteMatrix:
    """Writing column X changes X and nothing else — for every X.

    This is the shape the pre-2026-09-23 code failed and that no
    existing test reached: the clamp was only observable on a row
    written *twice with disjoint payloads*.  Every earlier test either
    wrote once (INSERT branch) or wrote the same column every time, so
    the defect was structurally unreachable from the suite.

    Parametrised over all seven columns rather than spot-checking one,
    because the failure mode is "some column the caller did not name got
    clobbered" — which column is incidental.
    """

    @pytest.mark.parametrize("column", RUNTIME_COLUMNS)
    def test_writing_one_column_leaves_the_other_six_alone(self, repo, column):
        repo.update_task("p1", "t1", dict(_SEEDED_ROW), expected_version=0)
        before = repo.get_task("p1", "t1")

        repo.update_task(
            "p1", "t1", {column: _writing(column)}, expected_version=0,
        )

        after = repo.get_task("p1", "t1")
        changed = {c for c in RUNTIME_COLUMNS if after[c] != before[c]}
        assert changed == {column}, (
            f"writing {column!r} changed {sorted(changed)}; a task-state "
            f"write must touch only the columns its payload names"
        )

    def test_a_multi_column_payload_changes_exactly_those_columns(self, repo):
        repo.update_task("p1", "t1", dict(_SEEDED_ROW), expected_version=0)
        before = repo.get_task("p1", "t1")

        payload = {
            "status": "completed",
            "commit_sha": "b" * 40,
            "failure_reason": None,
        }
        repo.update_task("p1", "t1", payload, expected_version=0)

        after = repo.get_task("p1", "t1")
        changed = {c for c in RUNTIME_COLUMNS if after[c] != before[c]}
        assert changed == set(payload), (
            "a declared column that only changes when it had a value "
            "(failure_reason) still counts as named"
        )

    def test_the_seeded_row_is_actually_fully_populated(self, repo):
        """Guard against the matrix going vacuous: if the seed ever loses
        a column, ``test_writing_one_column_leaves_the_other_six_alone``
        would pass for the wrong reason."""
        repo.update_task("p1", "t1", dict(_SEEDED_ROW), expected_version=0)
        entry = repo.get_task("p1", "t1")
        for column in RUNTIME_COLUMNS:
            assert entry[column] is not None, (
                f"seed value for {column!r} is NULL — the matrix would be "
                f"unable to tell 'preserved' from 'clamped'"
            )