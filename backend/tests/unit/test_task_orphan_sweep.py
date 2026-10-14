"""Boot recovery reclaims ``in_progress`` tasks whose owner process died.

The defect this pins
--------------------
``_mark_failed_dead`` is the single place where "the process that owns
this execution is gone" is concluded. It used to conclude that and stop
there: the ``plan_execution`` row went to ``failed`` and the plan phase
went to ``failed``, but every ``plan_tasks`` row the dead executor had
marked ``in_progress`` stayed exactly where it was. Nothing revisits
those rows -- no later sweep, no migration -- and the progress surfaces
read the row directly, so the plan kept reporting "currently running
<task>" indefinitely, backed by no executing process at all.

The fix hangs the reclaim off the same conclusion: once the idempotency
gate has admitted the call, the owner is known dead, so every
``in_progress`` row for that plan is residue and goes back to ``pending``.

Why these tests go through ``_recover_execution_states``
-------------------------------------------------------
The unit under test is not "a function that rewrites rows", it is the
boot funnel. Reaching ``_mark_failed_dead`` directly would prove the
helper works while leaving the liveness probe that actually gates it
untested -- and that probe is where the interesting cases live: an alive
PID must mean no reclaim, and a non-``running`` ``exec_status`` must
mean the funnel never gets that far. So the same
``_recover_execution_states(plans_dir)`` call the server makes at
startup is what drives these cases.

Hermeticity
-----------
Same contract as ``tests/test_server_recovery.py``: the ``plan_execution``
and ``plan_tasks`` rows are seeded through the per-test
``PDT_STATE_DB_PATH`` database that ``server._state_db_path()`` resolves,
never the operator's live ``state.db``. ``open()`` additionally
hard-refuses the live path under pytest, so a mis-pointed resolver fails
loudly instead of quietly mutating production state.
"""

import os
import sqlite3

import pytest

from plan_state import PlanState
from server import _execution_state, _recover_execution_states
from verification_loop import _mark_failed_dead, _reap_orphaned_in_progress_tasks


# A PID that is guaranteed not to be running. Chosen the same way the
# existing recovery suite picks one: a large value above any live
# system's ``pid_max`` default, so ``os.kill(pid, 0)`` raises
# ``ProcessLookupError`` rather than addressing some unrelated process.
DEAD_PID = 99999


def _seed_execution_row(plan_id, **fields):
    """Insert/update a ``plan_execution`` row in the hermetic test DB.

    Copied from ``tests/test_server_recovery.py`` -- writes through the
    production repository so the row shape matches what
    ``_recover_execution_states`` reads back via
    ``ExecutionRepository.summary``.
    """
    from server import _state_db_path
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = db_open(_state_db_path())
    try:
        migrate(conn)
        ExecutionRepository(conn).update_phase(
            plan_id,
            current_phase=fields.pop("current_phase", "executing"),
            create_if_missing=True,
            **fields,
        )
    finally:
        conn.close()


def _seed_task_row(plan_id, task_id, status):
    """Insert one ``plan_tasks`` row in the hermetic test DB.

    Raw INSERT rather than ``update_task``: ``update_task`` is
    INSERT-or-UPDATE, so it cannot distinguish "create this row" from
    "overwrite an existing row" -- and the tests below assert that rows
    the sweep must NOT touch come out unchanged.
    """
    from server import _state_db_path
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate

    conn = db_open(_state_db_path())
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO plan_tasks (plan_id, task_id, status, updated_at) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
            (plan_id, task_id, status),
        )
    finally:
        conn.close()


def _task_statuses(plan_id):
    """Return ``{task_id: status}`` for the plan, read back from disk."""
    from server import _state_db_path
    from state_machine.db.connection import open as db_open

    conn = db_open(_state_db_path())
    try:
        rows = conn.execute(
            "SELECT task_id, status FROM plan_tasks WHERE plan_id = ? "
            "ORDER BY task_id",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    return {row[0]: row[1] for row in rows}


class _ConnectionSpy:
    """Record every connection the code under test opens.

    Same shape as the spy in
    ``tests/unit/test_recovery_closes_its_connections.py`` -- patched on
    ``state_machine.db.connection.open`` rather than on ``close``, because
    ``sqlite3.Connection`` is an immutable C type and its methods cannot
    be replaced.
    """

    def __init__(self):
        self.opened = []
        self._real_open = conn_mod_open()

    def __call__(self, db_path):
        conn = self._real_open(db_path)
        self.opened.append(conn)
        return conn

    def leaked(self):
        return [c for c in self.opened if not _is_closed(c)]


def _is_closed(conn):
    """True when ``conn`` has been closed.

    ``sqlite3`` refuses any statement on a closed handle with
    ``ProgrammingError``; there is no public ``is_closed`` attribute, so
    the exception is the documented probe.
    """
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def conn_mod_open():
    """Unbound reference to the real ``connection.open``."""
    from state_machine.db.connection import open as _real
    return _real


def _seed_task_row_full(plan_id, task_id, **columns):
    """Insert one ``plan_tasks`` row with explicit runtime columns set.

    Used by the blast-radius test: ``update_task`` is a partial update, so
    the only way to prove the sweep writes ``status`` and nothing else is
    to give every other runtime column a non-NULL value first and read
    them all back afterwards.
    """
    from server import _state_db_path
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate

    columns.setdefault("status", "in_progress")
    columns.setdefault("updated_at", "2026-01-01T00:00:00Z")
    cols = ["plan_id", "task_id"] + list(columns)
    marks = ", ".join("?" for _ in cols)
    conn = db_open(_state_db_path())
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO plan_tasks ({0}) VALUES ({1})".format(
                ", ".join(cols), marks
            ),
            [plan_id, task_id] + list(columns.values()),
        )
    finally:
        conn.close()


def _task_row(plan_id, task_id):
    """Return the full runtime column set for one task row."""
    from server import _state_db_path
    from state_machine.db.connection import open as db_open

    cols = ("status", "end_ts", "attempt", "commit_sha", "failure_reason")
    conn = db_open(_state_db_path())
    try:
        row = conn.execute(
            "SELECT {0} FROM plan_tasks WHERE plan_id = ? AND task_id = ?".format(
                ", ".join(cols)
            ),
            (plan_id, task_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(zip(cols, row)) if row is not None else None


def _make_plan(sample_plan_factory, plan_id, phase="executing"):
    return sample_plan_factory(plan_id=plan_id, phase=phase)


def _sweep_lines(caplog):
    return [
        rec.getMessage() for rec in caplog.records
        if "[task_sweep]" in rec.getMessage()
    ]


class TestOrphanedInProgressIsReaped:
    """The dead-owner case -- the one that motivated the sweep."""

    def test_dead_pid_reaps_in_progress_to_pending(self, sample_plan_factory):
        plan_dir = _make_plan(sample_plan_factory, "reap-dead")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=DEAD_PID,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )
        _seed_task_row(plan_id, "repair-r1-11", "in_progress")

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)

        assert _execution_state[plan_id]["status"] == "failed"
        assert _task_statuses(plan_id) == {"repair-r1-11": "pending"}

    def test_alive_pid_leaves_row_untouched(self, sample_plan_factory):
        """A live owner means the sweep must not run at all.

        Mirrors ``tests/test_server_recovery.py::test_recover_running_alive``:
        ``os.getpid()`` is this process, so ``os.kill(pid, 0)`` succeeds
        and ``_mark_failed_dead`` is never reached.
        """
        plan_dir = _make_plan(sample_plan_factory, "reap-alive")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=os.getpid(),
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )
        _seed_task_row(plan_id, "repair-r1-11", "in_progress")

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)

        assert _execution_state[plan_id]["status"] == "running"
        assert _task_statuses(plan_id) == {"repair-r1-11": "in_progress"}

    @pytest.mark.parametrize("exec_status", ["completed", "failed"])
    def test_non_running_exec_status_short_circuits(
        self, sample_plan_factory, exec_status
    ):
        """The idempotency gate's ``status != "running"`` early return.

        The funnel never calls ``_mark_failed_dead`` at all for a
        non-running row, so an ``in_progress`` task row survives
        untouched even though the owning run has ended.
        """
        plan_dir = _make_plan(sample_plan_factory, "reap-" + exec_status)
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status=exec_status,
            exec_pid=DEAD_PID,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )
        _seed_task_row(plan_id, "task-1", "in_progress")

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)

        assert _execution_state[plan_id]["status"] == exec_status
        assert _task_statuses(plan_id) == {"task-1": "in_progress"}

    def test_two_in_progress_rows_are_both_reaped(self, sample_plan_factory):
        """Reaping is per-row, not "the one task the card happened to name"."""
        plan_dir = _make_plan(sample_plan_factory, "reap-two")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=DEAD_PID,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )
        _seed_task_row(plan_id, "task-1", "in_progress")
        _seed_task_row(plan_id, "repair-r1-11", "in_progress")

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)

        assert _task_statuses(plan_id) == {
            "task-1": "pending",
            "repair-r1-11": "pending",
        }

    def test_only_in_progress_rows_are_touched(self, sample_plan_factory):
        """Terminal verdicts and already-pending rows must survive intact.

        Asserted one row at a time rather than by comparing a whole
        dict, so a failure names the status that actually moved.
        """
        untouched = {
            "t-completed": "completed",
            "t-failed": "failed",
            "t-skipped": "skipped",
            "t-superseded": "superseded",
            "t-pending": "pending",
        }
        plan_dir = _make_plan(sample_plan_factory, "reap-selective")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=DEAD_PID,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )
        _seed_task_row(plan_id, "t-in-progress", "in_progress")
        for task_id, status in untouched.items():
            _seed_task_row(plan_id, task_id, status)

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)

        after = _task_statuses(plan_id)
        assert after["t-in-progress"] == "pending"
        for task_id, expected in untouched.items():
            assert after[task_id] == expected, task_id + " was modified"

    def test_another_plans_in_progress_row_is_untouched(
        self, sample_plan_factory
    ):
        """The sweep is scoped to the dead plan, not the whole table."""
        plans_root = sample_plan_factory(
            plan_id="reap-mine", phase="executing"
        ).parent
        dead_dir = plans_root / "reap-mine"
        live_dir = _make_plan(sample_plan_factory, "reap-theirs")
        for plan_dir, pid in ((dead_dir, DEAD_PID),
                              (live_dir, os.getpid())):
            _seed_execution_row(
                plan_dir.name,
                exec_status="running",
                exec_pid=pid,
                started_at="2024-01-01T00:00:00",
                project_dir=str(plan_dir / "project"),
            )
            _seed_task_row(plan_dir.name, "task-1", "in_progress")

        _execution_state.clear()
        _recover_execution_states(plans_root)

        assert _task_statuses(dead_dir.name) == {"task-1": "pending"}
        assert _task_statuses(live_dir.name) == {"task-1": "in_progress"}

    def test_second_boot_is_a_no_op(self, sample_plan_factory):
        """Idempotence: with no ``in_progress`` left, the second pass
        finds nothing, logs nothing, and changes nothing."""
        plan_dir = _make_plan(sample_plan_factory, "reap-twice")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=DEAD_PID,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )
        _seed_task_row(plan_id, "task-1", "in_progress")

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)
        first = _task_statuses(plan_id)

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)

        assert _task_statuses(plan_id) == first == {"task-1": "pending"}

    def test_reap_writes_only_the_status_column(self, sample_plan_factory):
        """Blast radius: ``status`` and nothing else.

        The reap must not leave terminal traces behind. ``end_ts`` or
        ``failure_reason`` on the row would assert that the task ran to
        some conclusion, and it did not — it was interrupted. This is
        the column-level half of the same claim the target-status choice
        makes; the status half is ``test_reap_writes_only_the_status_column``'s
        sibling, ``test_dead_pid_reaps_in_progress_to_pending``.
        """
        plan_dir = _make_plan(sample_plan_factory, "reap-blast")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=DEAD_PID,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )
        _seed_task_row_full(
            plan_id,
            "task-1",
            end_ts="2024-01-01T00:05:00Z",
            attempt=2,
            commit_sha="abc123",
            failure_reason="interrupted by the sweep test",
        )

        before = _task_row(plan_id, "task-1")
        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)
        after = _task_row(plan_id, "task-1")

        assert before["status"] == "in_progress"
        assert after["status"] == "pending"
        for column in ("end_ts", "attempt", "commit_sha", "failure_reason"):
            assert after[column] == before[column], column + " was clobbered"

    def test_reap_logs_the_task_ids_it_reclaimed(
        self, sample_plan_factory, caplog
    ):
        plan_dir = _make_plan(sample_plan_factory, "reap-log")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=DEAD_PID,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )
        _seed_task_row(plan_id, "repair-r1-11", "in_progress")

        _execution_state.clear()
        with caplog.at_level("WARNING"):
            _recover_execution_states(plan_dir.parent)

        lines = _sweep_lines(caplog)
        assert len(lines) == 1, lines
        assert "reaped 1 orphaned in_progress task(s)" in lines[0]
        assert "repair-r1-11" in lines[0]

    def test_log_truncates_a_wide_reap(self, sample_plan_factory, caplog):
        """A plan with more interrupted tasks than one log line can name
        still says how many there were, and how many it omitted."""
        from verification_loop import _TASK_SWEEP_LOG_ID_LIMIT

        plan_dir = _make_plan(sample_plan_factory, "reap-wide")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=DEAD_PID,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )
        total = _TASK_SWEEP_LOG_ID_LIMIT + 3
        for i in range(total):
            _seed_task_row(plan_id, "task-{0:03d}".format(i), "in_progress")

        _execution_state.clear()
        with caplog.at_level("WARNING"):
            _recover_execution_states(plan_dir.parent)

        line = _sweep_lines(caplog)[0]
        assert "reaped {0} orphaned in_progress task(s)".format(total) in line
        assert "+3 more" in line
        # Every row really was reclaimed even though the line is short.
        assert set(_task_statuses(plan_id).values()) == {"pending"}


class TestSweepCannotBreakRecovery:
    """Requirement: a failed reap must not abort recovery.

    ``_mark_failed_dead`` is the funnel every dead-owner path goes
    through. If the sweep can raise, it takes the execution-row write
    and the plan-phase transition down with it -- recovery would fail
    *because* it tried to clean up after recovery.
    """

    def test_update_task_failure_does_not_propagate(
        self, sample_plan_factory, monkeypatch
    ):
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )

        plan_dir = _make_plan(sample_plan_factory, "reap-write-fail")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=DEAD_PID,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )
        _seed_task_row(plan_id, "task-1", "in_progress")

        def _boom(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(PlanTaskRepository, "update_task", _boom)

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)  # must not raise

        assert _execution_state[plan_id]["status"] == "failed"
        assert PlanState(plan_dir).get_current_phase() == "failed"

    def test_missing_plan_tasks_table_does_not_propagate(
        self, sample_plan_factory, monkeypatch
    ):
        """A state DB with no ``plan_tasks`` table at all.

        Both ``_mark_failed_dead`` and the sweep migrate before touching
        their tables, so migration is neutralised here and the table
        dropped -- otherwise the sweep would silently re-create it and
        the case would never be exercised.
        """
        from server import _state_db_path
        from state_machine.db.connection import open as db_open
        from state_machine.db import schema as schema_mod

        plan_dir = _make_plan(sample_plan_factory, "reap-no-table")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=DEAD_PID,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )

        conn = db_open(_state_db_path())
        try:
            conn.execute("DROP TABLE IF EXISTS plan_tasks")
        finally:
            conn.close()
        monkeypatch.setattr(schema_mod, "migrate", lambda conn: None)

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)  # must not raise

        assert _execution_state[plan_id]["status"] == "failed"
        assert PlanState(plan_dir).get_current_phase() == "failed"

    def test_unopenable_db_is_swallowed_by_the_helper(self, monkeypatch):
        """The sweep's own open failure is caught by the sweep.

        Scoped to the helper rather than the whole funnel on purpose:
        ``_mark_failed_dead`` opens the same database for its
        ``plan_execution`` write *before* the sweep runs, so a database
        neither can open is a pre-existing condition of that function,
        not something the sweep introduces. What is new -- and what is
        pinned here -- is that the sweep adds no second way to fail.
        """
        import server as server_mod

        blocker = str(server_mod._state_db_path().parent / "not-a-dir")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("")
        monkeypatch.setattr(
            server_mod,
            "_state_db_path",
            lambda request=None: os.path.join(blocker, "state.db"),
        )

        assert _reap_orphaned_in_progress_tasks("any-plan") == 0

    def test_reap_helper_closes_its_connection(self, sample_plan_factory):
        """Connection hygiene -- this repo has a leak history.

        Uses the same probe as
        ``tests/unit/test_recovery_closes_its_connections.py``: ``sqlite3``
        refuses statements on a closed handle, so ``ProgrammingError`` is
        the documented way to ask whether a connection is still live.
        Spying on ``connection.open`` -- the single chokepoint every
        reader and writer goes through -- scopes the assertion to the
        sweep's own handle, so unrelated cleanup cannot mask a leak.
        """
        from state_machine.db import connection as conn_mod

        plan_dir = _make_plan(sample_plan_factory, "reap-conns")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=DEAD_PID,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )
        _seed_task_row(plan_id, "task-1", "in_progress")

        spy = _ConnectionSpy()
        monkey = pytest.MonkeyPatch()
        monkey.setattr(conn_mod, "open", spy)
        try:
            _reap_orphaned_in_progress_tasks(plan_id)
        finally:
            monkey.undo()

        assert len(spy.opened) == 1, spy.opened
        assert spy.leaked() == []

    def test_reap_helper_closes_connection_when_write_fails(
        self, sample_plan_factory
    ):
        """The ``finally`` must fire on the failure path too."""
        from state_machine.db import connection as conn_mod
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )

        plan_dir = _make_plan(sample_plan_factory, "reap-conn-fail")
        plan_id = plan_dir.name
        _seed_task_row(plan_id, "task-1", "in_progress")

        spy = _ConnectionSpy()
        monkey = pytest.MonkeyPatch()
        monkey.setattr(conn_mod, "open", spy)
        monkey.setattr(
            PlanTaskRepository, "update_task",
            lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("nope")),
        )
        try:
            assert _reap_orphaned_in_progress_tasks(plan_id) == 0
        finally:
            monkey.undo()

        assert len(spy.opened) == 1, spy.opened
        assert spy.leaked() == []


class TestSchedulingSemanticsAreUnchanged:
    """Why ``pending`` and not ``failed`` -- asserted, not just asserted
    in a comment.

    The whole justification for the target status is that ``pending``
    and ``in_progress`` are indistinguishable to the scheduler. If that
    ever stops being true, the sweep would silently start changing
    execution behaviour, so the invariant is pinned against the real
    sets rather than against a copy of the comment.
    """

    def test_pending_and_in_progress_share_a_scheduling_class(self):
        from agent import _TERMINAL_TASK_STATUSES

        assert "in_progress" not in _TERMINAL_TASK_STATUSES
        assert "pending" not in _TERMINAL_TASK_STATUSES
        # And ``failed`` -- the status we deliberately did NOT write --
        # is terminal, which is exactly why writing it would have been
        # wrong: it would drop the task out of the DAG for good.
        assert "failed" in _TERMINAL_TASK_STATUSES

    def test_reaped_task_is_still_runnable_by_the_loader(
        self, sample_plan_factory
    ):
        """End of the chain: the reaped row still reaches ``_load_tasks``."""
        from agent import _TERMINAL_TASK_STATUSES

        plan_dir = _make_plan(sample_plan_factory, "reap-dag")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=DEAD_PID,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )
        _seed_task_row(plan_id, "downstream-1", "in_progress")

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)

        status = _task_statuses(plan_id)["downstream-1"]
        assert status == "pending"
        assert status not in _TERMINAL_TASK_STATUSES


class TestDirectHelperContract:
    """``_mark_failed_dead`` is also reached directly by the runtime
    watchdog (``_lazy_check_execution``), so the helper and the
    idempotency gate are worth pinning on their own terms too."""

    def test_helper_on_plan_with_no_task_rows(self):
        assert _reap_orphaned_in_progress_tasks("no-such-plan") == 0

    def test_mark_failed_dead_early_returns_on_terminal_state(
        self, sample_plan_factory
    ):
        """Idempotency gate: a non-``running`` in-memory state returns
        before the sweep, so nothing is rewritten."""
        plan_dir = _make_plan(sample_plan_factory, "reap-gate")
        plan_id = plan_dir.name
        _seed_task_row(plan_id, "task-1", "in_progress")
        _execution_state.clear()
        _execution_state[plan_id] = {"status": "completed", "pid": None}
        try:
            _mark_failed_dead(plan_id)
            assert _task_statuses(plan_id) == {"task-1": "in_progress"}
        finally:
            _execution_state.clear()
