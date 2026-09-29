"""A finished task must not be re-run just because a row went missing.

2026-09-22, a production plan
--------------------------------------------------
The run finished 16 tasks and then re-executed 12 of them, twice, for
~2h of wasted provider quota. The refiner was blamed first; it is not
guilty. ``_apply_refiner_structure`` explicitly ``continue``s for every
task that already exists, and ``refiner.py`` never touches runtime
state at all.

The real shape of the bug is a single source of truth that can vanish:

  * ``tasks.json`` carries NO status — ``TaskManager.save_tasks`` strips
    ``status`` / ``updated_time`` / ``failure_reason`` /
    ``breakdown_count`` on every write (task #3.8 moved runtime state to
    SQLite). So the file that survives a task-list rewrite says nothing
    about what already finished.
  * the only place a terminal status can live is the ``plan_tasks`` row,
  * and the hydration in ``_load_tasks`` silently fell through to
    ``pending`` whenever that row was missing or NULL:

      if persisted and persisted.get("status"):   # ← falsy: skip
          t.status = persisted["status"]

Measured on the real database: the tasks with ``status IS NULL`` were
exactly ``8, 9, 10, 11, 13, 14-1, 14-4, 7-1..7-4`` — and the tasks that
were re-dispatched were exactly that set. The tasks with a populated
status (``1..6``, ``12``) were not re-run once.

So these tests pin three things: the loader never silently converts a
missing row into a re-run, the write paths never silently drop the
mirror, and ``add_task`` never resets a row it did not create.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from task_manager import TaskManager  # noqa: E402

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)


#: ``TaskManager._persist_status_to_sqlite`` derives the plan id from
#: ``derive_plan_id_from_tasks_file(tasks_file)`` — i.e. the parent
#: directory name. A random ``tmp_path`` would write under a different
#: namespace and every assertion here would pass vacuously.
PLAN_ID = "test-plan-status-survives"


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


class _LogRecorder:
    """Captures the structured events the executor would emit."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def _record(self, level: str):
        def _fn(event: str, message: str, **kwargs: Any) -> None:
            self.events.append({
                "level": level.upper(),
                "event": event,
                "message": message,
                "data": kwargs.get("data"),
            })
        return _fn

    def __getattr__(self, name: str):
        if name in ("info", "warning", "error", "debug", "critical"):
            return self._record(name)
        raise AttributeError(name)

    def events_named(self, event: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]


class _StubCodingTool:
    """``_load_tasks`` must never need an LLM."""

    def query(self, *a: Any, **k: Any):  # pragma: no cover - must not run
        raise AssertionError("_load_tasks must not call the coding tool")

    query_json = query


@pytest.fixture
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    d = tmp_path / PLAN_ID
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(d / "state.db"))
    yield d


@pytest.fixture
def conn(plan_dir: Path) -> Iterator[sqlite3.Connection]:
    c = open_db(plan_dir / "state.db")
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _write_tasks(plan_dir: Path, tasks: List[Dict[str, Any]]) -> Path:
    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text(
        json.dumps({"requirement": "r", "tasks": tasks}, ensure_ascii=False),
        encoding="utf-8",
    )
    return tasks_file


def _manager(plan_dir: Path, logger: Any = None) -> TaskManager:
    tm = TaskManager(project_dir=plan_dir, tasks_file=plan_dir / "tasks.json")
    if logger is not None:
        tm.logger = logger
    tm.load_tasks()
    return tm


# ---------------------------------------------------------------------------
# 1c — add_task must not reset a row it did not create
# ---------------------------------------------------------------------------


class TestAddTaskPreservesExistingRuntimeState:
    def test_re_adding_a_completed_id_does_not_reset_it(self, conn):
        """The upsert used to carry ``status = excluded.status`` with
        ``excluded.status`` pinned to ``"pending"``. So *any* code path
        that re-added an id silently reset a finished task to "not
        started yet" — and with the disk file carrying no status, that
        was the only surviving copy of the truth."""
        repo = PlanTaskRepository(conn)
        repo.add_task(PLAN_ID, _task("1"))
        repo.update_task(
            PLAN_ID, "1",
            {"status": "completed", "end_ts": "2026-09-22T05:00:00Z"},
        )

        repo.add_task(PLAN_ID, _task("1", title="renamed"))

        entry = repo.get_task(PLAN_ID, "1")
        assert entry["status"] == "completed"
        assert entry["end_ts"] == "2026-09-22T05:00:00Z"
        # Static content still updates — only runtime state is protected.
        assert entry["title"] == "renamed"

    def test_a_genuinely_new_row_still_starts_pending(self, conn):
        repo = PlanTaskRepository(conn)
        version = repo.add_task(PLAN_ID, _task("9"))
        assert version == 1
        assert repo.get_task(PLAN_ID, "9")["status"] == "pending"

    def test_a_failed_row_is_not_reset_either(self, conn):
        repo = PlanTaskRepository(conn)
        repo.add_task(PLAN_ID, _task("2"))
        repo.update_task(
            PLAN_ID, "2",
            {"status": "failed", "failure_reason": "pytest exit 1"},
        )

        repo.add_task(PLAN_ID, _task("2"))

        entry = repo.get_task(PLAN_ID, "2")
        assert entry["status"] == "failed"
        assert entry["failure_reason"] == "pytest exit 1"


# ---------------------------------------------------------------------------
# 1b — the SQLite mirror must not be skipped silently
# ---------------------------------------------------------------------------


class TestMirrorSkipIsLoud:
    def test_status_write_for_an_unknown_task_is_logged(self, plan_dir):
        """Skipping is legitimate (the refiner dropped the task and
        re-creating the row would resurrect it), so this must not
        raise. It must not be invisible either: with no status on disk,
        a skipped mirror is indistinguishable from "never completed" on
        the next reload."""
        logger = _LogRecorder()
        _write_tasks(plan_dir, [_task("1")])
        tm = _manager(plan_dir, logger)

        tm.update_task_status("not-in-this-plan", "completed")

        events = logger.events_named("task_persist_skipped_unknown_task")
        assert len(events) == 1
        assert events[0]["level"] == "ERROR"
        assert events[0]["data"]["status"] == "completed"

    def test_a_known_task_is_not_reported_as_skipped(self, plan_dir, conn):
        logger = _LogRecorder()
        _write_tasks(plan_dir, [_task("1")])
        tm = _manager(plan_dir, logger)

        tm.update_task_status("1", "completed")

        assert not logger.events_named("task_persist_skipped_unknown_task")
        assert PlanTaskRepository(conn).get_task(PLAN_ID, "1")["status"] == (
            "completed"
        )


# ---------------------------------------------------------------------------
# 1d — the write must round-trip
# ---------------------------------------------------------------------------


class TestWriteRoundTrip:
    def test_a_clobbering_writer_is_detected(self, plan_dir, conn, monkeypatch):
        """``update_task`` is last-writer-wins. If something clobbers the
        row between our write and our read-back, the loss is otherwise
        invisible until the next reload re-runs the task."""
        logger = _LogRecorder()
        _write_tasks(plan_dir, [_task("1")])
        tm = _manager(plan_dir, logger)
        repo = PlanTaskRepository(conn)

        real_update = PlanTaskRepository.update_task

        def _clobbering_update(self, plan_id, task_id, fields, expected_version=0):
            real_update(self, plan_id, task_id, fields, expected_version)
            # A concurrent refiner structure pass resetting the row.
            real_update(self, plan_id, task_id, {"status": "pending"})

        monkeypatch.setattr(PlanTaskRepository, "update_task", _clobbering_update)

        tm.update_task_status("1", "completed")

        assert logger.events_named("task_persist_readback_mismatch"), (
            "a status write that did not survive its own round-trip must "
            "be reported; otherwise the loss only shows up as a re-run"
        )
        assert repo.get_task(PLAN_ID, "1")["status"] == "pending"


# ---------------------------------------------------------------------------
# 1d — the 0923 re-run: a partial write must not erase what it did not name
# ---------------------------------------------------------------------------


class TestCommitShaWriteKeepsTheCompletedStatus:
    """``_commit_task_changes`` runs moments after the completion write.

    ``agent.process_task`` does::

        self.task_manager.update_task_status(task.id, "completed")   # 4704
        ...
        self._commit_task_changes(task, changed_files)               # 4715

    and ``update_task_commit_sha`` mirrors ``{"commit_sha": sha}`` into
    ``plan_tasks``.  While ``PlanTaskRepository.update_task`` clamped
    every runtime column the payload omitted to NULL, that second write
    erased the ``completed`` the first one had just recorded.

    A commit write that lands on top of a completion leaves the row with
    ``status IS NULL`` and a populated ``commit_sha`` — the completion is
    erased.  The card, which counts ``status = 'completed'``, then
    reports nothing as finished for the whole run, and the tasks are
    re-dispatched.
    """

    def test_recording_the_commit_keeps_the_completion(
        self, plan_dir, conn,
    ):
        _write_tasks(plan_dir, [_task("1")])
        tm = _manager(plan_dir)
        repo = PlanTaskRepository(conn)

        tm.update_task_status("1", "completed")
        tm.update_task_commit_sha("1", "d" * 40)

        entry = repo.get_task(PLAN_ID, "1")
        assert entry["status"] == "completed", (
            "recording the commit must not erase the completion it proves"
        )
        assert entry["commit_sha"] == "d" * 40
        assert entry["end_ts"], "the completion timestamp must survive too"

    def test_completing_a_failed_task_still_clears_the_reason(
        self, plan_dir, conn,
    ):
        """The clearing that used to fall out of the clobber is now
        explicit: a non-``failed`` status clears ``failure_reason``."""
        _write_tasks(plan_dir, [_task("1")])
        tm = _manager(plan_dir)
        repo = PlanTaskRepository(conn)

        tm.record_task_failure("1", "boom")
        assert repo.get_task(PLAN_ID, "1")["failure_reason"] == "boom"

        tm.update_task_status("1", "completed")
        entry = repo.get_task(PLAN_ID, "1")
        assert entry["status"] == "completed"
        assert entry["failure_reason"] is None, (
            "a fixed failure must not be fed back to the model on a "
            "later re-dispatch"
        )

    def test_a_completed_row_still_hydrates_as_completed(
        self, plan_dir, conn,
    ):
        """End of the chain: after the commit write, the reload the
        dispatcher actually performs still sees a terminal task."""
        logger = _LogRecorder()
        _write_tasks(plan_dir, [_task("1")])
        tm = _manager(plan_dir)
        tm.update_task_status("1", "completed")
        tm.update_task_commit_sha("1", "e" * 40)

        agent = _agent_for(plan_dir, logger)
        agent._load_tasks()

        by_id = {t.id: t.status for t in agent._all_tasks}
        assert by_id["1"] == "completed"
        assert not logger.events_named("task_status_missing_on_reload")


# ---------------------------------------------------------------------------
# 1a — the loader, which is where 0921 actually lost the work
# ---------------------------------------------------------------------------


def _agent_for(plan_dir: Path, logger: Any):
    """Build a real ``AutonomousAgent`` bound to ``plan_dir``.

    ``AutonomousAgent.__init__`` builds a ``GitManager`` against
    ``project_dir`` and requires it to be a repository, so the fixture
    directory is initialised first.
    """
    import git

    from agent import AutonomousAgent

    try:
        git.Repo(plan_dir)
    except git.InvalidGitRepositoryError:
        git.Repo.init(plan_dir)

    agent = AutonomousAgent(
        requirement="r",
        project_dir=plan_dir,
        coding_tool=_StubCodingTool(),
        logger=logger,
        tasks_file=plan_dir / "tasks.json",
    )
    return agent


def _null_out(conn: sqlite3.Connection, task_id: str) -> None:
    """Reproduce the observed 0921 state: a row that exists but whose
    status is NULL (an ``add_task`` insert that never carried a status,
    followed by no runtime write)."""
    conn.execute(
        "UPDATE plan_tasks SET status = NULL WHERE plan_id = ? AND task_id = ?",
        (PLAN_ID, task_id),
    )
    conn.commit()


class TestLoaderNeverSilentlyReruns:
    def test_a_completed_row_hydrates(self, plan_dir, conn):
        """Baseline: the happy path still works."""
        logger = _LogRecorder()
        _write_tasks(plan_dir, [_task("1"), _task("2")])
        tm = _manager(plan_dir)
        tm.update_task_status("1", "completed")

        agent = _agent_for(plan_dir, logger)
        agent._load_tasks()

        by_id = {t.id: t.status for t in agent._all_tasks}
        assert by_id["1"] == "completed"
        assert by_id["2"] == "pending"
        assert not logger.events_named("task_status_missing_on_reload")

    def test_a_null_row_is_reported_not_silently_re_run(self, plan_dir, conn):
        logger = _LogRecorder()
        _write_tasks(plan_dir, [_task("1"), _task("2")])
        tm = _manager(plan_dir)
        tm.update_task_status("1", "completed")
        _null_out(conn, "1")

        agent = _agent_for(plan_dir, logger)
        agent._load_tasks()

        events = logger.events_named("task_status_missing_on_reload")
        assert events, (
            "a task reloaded without a runtime status row vanished "
            "silently — this is the exact signal that was missing while "
            "the 0921 run re-executed eight finished tasks"
        )
        assert events[0]["data"]["task_ids"] == ["1"]

    def test_work_completed_this_session_is_never_re_run(self, plan_dir, conn):
        """The belt-and-braces guard. Even with the row unusable, a task
        this session already finished stays finished — a missing row is
        not evidence that the work did not happen."""
        logger = _LogRecorder()
        _write_tasks(plan_dir, [_task("1"), _task("2")])
        tm = _manager(plan_dir)
        tm.update_task_status("1", "completed")
        _null_out(conn, "1")

        agent = _agent_for(plan_dir, logger)
        agent._session_task_completed_counts = {"1": 1}
        agent._load_tasks()

        by_id = {t.id: t.status for t in agent._all_tasks}
        assert by_id["1"] == "completed"
        assert by_id["2"] == "pending"
        assert logger.events_named("task_status_recovered_from_session")

    def test_a_task_that_never_ran_is_still_schedulable(self, plan_dir, conn):
        """The guard must not freeze work that legitimately has not
        happened yet."""
        logger = _LogRecorder()
        _write_tasks(plan_dir, [_task("1"), _task("2")])

        agent = _agent_for(plan_dir, logger)
        agent._session_task_completed_counts = {"some-other-id": 1}
        agent._load_tasks()

        by_id = {t.id: t.status for t in agent._all_tasks}
        assert by_id["1"] == "pending"
        assert by_id["2"] == "pending"
        assert not logger.events_named("task_status_recovered_from_session")


class TestRefinerRewriteKeepsFinishedWork:
    """The end-to-end shape of the 0921 incident.

    A refiner splits one task; the whole task list is rewritten to disk
    without status (by design); ``_load_tasks`` then re-reads it. Every
    task that already finished must still read as finished afterwards —
    that is what failed, and it re-dispatched twelve tasks.
    """

    def test_split_then_reload_keeps_completed_statuses(self, plan_dir, conn):
        logger = _LogRecorder()
        _write_tasks(plan_dir, [_task("1"), _task("2"), _task("3")])
        tm = _manager(plan_dir)
        agent = _agent_for(plan_dir, logger)
        agent._load_tasks()

        # Waves 1..n finish tasks 1 and 2.
        tm.update_task_status("1", "completed")
        tm.update_task_status("2", "completed")

        # The refiner splits task 3 into 3-1 / 3-2 and rewrites the list.
        current = [t.model_dump() for t in tm.tasks]
        current = [t for t in current if t["id"] != "3"]
        current.append(_task("3-1", depends_on=["2"]))
        current.append(_task("3-2", depends_on=["3-1"]))
        tm.set_tasks(current)

        # The rewrite is exactly where 0921 lost everything.
        agent._load_tasks()

        by_id = {t.id: t.status for t in agent._all_tasks}
        assert by_id["1"] == "completed"
        assert by_id["2"] == "completed"
        assert by_id["3-1"] == "pending"
        assert by_id["3-2"] == "pending"
