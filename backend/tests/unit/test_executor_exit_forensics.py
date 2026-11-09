"""The parent writes the crash scene the executor could not write itself.

Background
----------
A SIGKILLed executor never gets to write anything down.  The process is
gone between one instruction and the next, so the report that would
have said *which task was running, how long had it been up, was it
killed or did it fail* is never written.  ``plan_execution.flags``
comes back empty and ``logs/executor_*.log`` stops half way through a
sentence -- the questions have no answer, and the answer is not
reconstructed from the plan afterwards because the evidence was never
written down.

The repair has two halves.  ``backend/crash_forensics.py`` is the
writing half (pure functions, already in place).  This file pins the
*wiring* half: the parent -- the process that launched the executor and
therefore outlives it -- writes the fallback report and records it in
the plan's flags.

What is pinned
--------------
* a signal death is described as a **signal** (``signal=9``,
  ``signal_name='SIGKILL'``, ``killed_by='signal'``), not as an exit
  code;
* the fallback says where it came from (``source='parent'``), so a
  reader can tell it from the executor's own report;
* the command line is copied off the process handle, item for item;
* a report the executor wrote itself is **reused**, not duplicated;
* a report left over from an earlier run (a different pid) is not
  mistaken for this run's report;
* the flags column keeps the keys it already had -- the crash is one
  more key among them, not a replacement of them;
* a clean exit writes nothing at all;
* the report's ``last_events`` are the raw lines of ``execution.log``,
  and the task id is read out of them, not invented.

The boundary cases matter as much as the happy path: a plan directory
that cannot be written, a database that cannot be opened, a flags
column that is empty or ``NULL``.  None of them may change the run's
status or its exit code -- a missing report is much better than a
second failure on the way out of a run that has already died.

TDD spec -- 6 contract tests
-----------------------------
1. ``test_parent_writes_fallback_when_no_report`` -- rc=-9 -> a file
   with ``signal=9`` / ``killed_by='signal'`` / ``source='parent'``.
2. ``test_parent_records_argv_from_process_args`` -- the argv is
   copied item for item.
3. ``test_parent_reuses_executor_report`` -- the executor's own report
   is reused; no second file; the flags come from it.
4. ``test_parent_ignores_stale_report_from_other_pid`` -- a report
   from another pid is treated as absent; a new one is written.
5. ``test_parent_persists_flags_without_clobbering`` -- the flags the
   row already had are still there afterwards.
6. ``test_parent_silent_on_clean_exit`` -- rc=0 -> no file, no flags.

Boundary cases (outside the six):
``test_parent_flags_merge_into_empty_flags``,
``test_parent_report_path_is_relative_to_the_plan_dir``,
``test_parent_reads_last_events_from_the_execution_log``,
``test_parent_survives_an_unwritable_plan_dir``,
``test_parent_survives_an_unreachable_database``,
``test_persist_crash_flags_merges_without_clobbering``.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` must be imported first: it seeds ``sys.modules['server']``
# and then wires the ``routes`` package, and ``routes.execution``
# late-binds back into it.
import server  # noqa: E402,F401
from routes.execution import (  # noqa: E402
    _finalize_executor_forensics,
    _persist_crash_flags,
)
from crash_forensics import (  # noqa: E402
    build_crash_report,
    crash_report_path,
    write_crash_report,
)
from utils.atomic_io import atomic_write_json  # noqa: E402

PLAN_ID = "executor-exit-forensics-plan"

#: A moment safely in the past, so a report written under this name is
#: always older than one the parent writes now.
_PAST = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeProcess:
    """The executor handle the parent holds: a pid, a return code, a command."""

    def __init__(
        self,
        returncode: Optional[int],
        *,
        pid: int = 4242,
        args: Optional[List[str]] = None,
    ) -> None:
        self.pid = pid
        self.returncode = returncode
        self.args = list(args) if args is not None else [
            "/venv/bin/python3",
            "backend/cli.py",
            "--recover",
        ]

    def poll(self) -> Optional[int]:
        return self.returncode

    def wait(self) -> Optional[int]:
        return self.returncode


def _seed_execution_row(
    tmp_path: Path,
    *,
    flags: Optional[Dict[str, Any]] = None,
    flags_null: bool = False,
    stop_reason: str = "startup_reconcile",
) -> Path:
    """Create the ``plan_execution`` row the flags are written into.

    Returns the path of the hermetic ``state.db`` the row lives in.
    The suite redirects ``server._state_db_path`` at the same file.

    ``flags_null`` writes a NULL flags column rather than an empty
    JSON object -- the two are different on disk and the reader has to
    treat them the same way.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution "
            "(plan_id, attempt_count, project_dir, stop_reason, task_progress, "
            " next_run_at, card_state, flags, exec_pid, exec_status, "
            " started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                PLAN_ID,
                0,
                str(tmp_path / "project"),
                stop_reason,
                json.dumps({"tasks": {}}),
                None,
                "{}",
                None if flags_null else json.dumps(
                    flags if flags is not None else {}
                ),
                None,
                "running",
                "2026-11-08T12:00:00+00:00",
                "2026-11-08T12:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _read_row(db_path: Path) -> Dict[str, Any]:
    """Read the plan's ``plan_execution`` row: its flags and its reason."""
    from state_machine.db.connection import open as open_db

    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT flags, stop_reason FROM plan_execution WHERE plan_id = ?",
            (PLAN_ID,),
        )
        row = cur.fetchone()
        assert row is not None, "the plan has no execution row"
        flags = json.loads(row[0]) if row[0] else None
        return {"flags": flags, "stop_reason": row[1]}
    finally:
        conn.close()


def _crash_files(plan_dir: Path) -> List[Path]:
    """The crash reports written under ``plan_dir``, oldest name first."""
    logs = plan_dir / "logs"
    if not logs.is_dir():
        return []
    return sorted(logs.glob("executor_crash_*.json"), key=lambda item: item.name)


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """A plan directory, with an execution row behind it in the database."""
    _seed_execution_row(tmp_path)
    directory = tmp_path / "plans" / PLAN_ID
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# ---------------------------------------------------------------------------
# 1. the fallback report
# ---------------------------------------------------------------------------


def test_parent_writes_fallback_when_no_report(plan_dir: Path) -> None:
    """rc=-9 -> the parent writes the scene: signal 9, killed by a signal.

    The executor had no chance to write anything, so the parent does it
    from the facts it holds: the return code, the pid, the command line.
    The report says where it came from -- ``source='parent'`` -- so a
    reader can tell it from the executor's own report.
    """
    process = _FakeProcess(-9)

    report = _finalize_executor_forensics(
        PLAN_ID,
        plan_dir,
        process,
        started_at="2026-11-08T12:00:00+00:00",
    )

    assert report is not None
    assert report["signal"] == 9
    assert report["signal_name"] == "SIGKILL"
    assert report["killed_by"] == "signal"
    assert report["exit_code"] is None
    assert report["source"] == "parent"
    assert report["pid"] == 4242
    assert report["plan_id"] == PLAN_ID

    files = _crash_files(plan_dir)
    assert len(files) == 1, "the parent wrote no report (or more than one)"
    on_disk = json.loads(files[0].read_text(encoding="utf-8"))
    assert on_disk == report, "the file on disk is not the report returned"


def test_parent_records_argv_from_process_args(plan_dir: Path) -> None:
    """The command line is copied off the handle, item for item.

    The argv is what tells a reader *which* run died -- the same plan
    can be started with different arguments, and the report is the only
    place that difference is written down.
    """
    argv = [
        "/venv/bin/python3",
        "backend/cli.py",
        "--plan",
        "vp001",
        "--recover",
    ]
    process = _FakeProcess(-9, args=argv)

    report = _finalize_executor_forensics(PLAN_ID, plan_dir, process)

    assert report is not None
    assert report["argv"] == argv


# ---------------------------------------------------------------------------
# 2. the executor's own report wins
# ---------------------------------------------------------------------------


def test_parent_reuses_executor_report(plan_dir: Path, tmp_path: Path) -> None:
    """The executor wrote a report itself -> reuse it, do not duplicate it.

    The executor's report carries what only it knows (the traceback,
    the stack).  A second file next to it would be a second answer to
    the same question, and the newer one -- the poorer one -- would be
    the one a reader finds first.
    """
    executor_report = build_crash_report(
        plan_dir=plan_dir,
        plan_id=PLAN_ID,
        pid=4242,
        argv=["/venv/bin/python3", "backend/cli.py"],
        started_at="2026-11-08T12:00:00+00:00",
        crashed_at="2026-11-08T12:05:00+00:00",
        uptime_seconds=300.0,
        last_events=['{"task_id": "3-2"}'],
        active_task_id="3-2",
        traceback="Traceback (most recent call last): ...",
        faulthandler="Stack (most recent call last): ...",
        source="executor",
        exit_code=None,
        signal=9,
        signal_name="SIGKILL",
        killed_by="signal",
    )
    assert write_crash_report(plan_dir, executor_report) is not None

    report = _finalize_executor_forensics(PLAN_ID, plan_dir, _FakeProcess(-9))

    assert report == executor_report
    assert len(_crash_files(plan_dir)) == 1, "the parent wrote a second report"

    crash = _read_row(tmp_path / "state.db")["flags"]["crash"]
    assert crash["signal"] == 9
    assert crash["signal_name"] == "SIGKILL"
    assert crash["killed_by"] == "signal"


def test_parent_ignores_stale_report_from_other_pid(plan_dir: Path) -> None:
    """A report from another pid is last run's leftover -> write a new one.

    The plan directory is reused across runs.  A report that names a
    different pid describes a different process, and reusing it would
    answer the question "what happened to *this* run" with the story of
    the previous one.
    """
    stale = build_crash_report(
        plan_dir=plan_dir,
        plan_id=PLAN_ID,
        pid=999,
        started_at="2026-11-08T11:00:00+00:00",
        crashed_at="2026-11-08T11:05:00+00:00",
        uptime_seconds=300.0,
        source="executor",
        exit_code=None,
        signal=9,
        signal_name="SIGKILL",
        killed_by="signal",
    )
    stale_path = crash_report_path(plan_dir, now=_PAST)
    atomic_write_json(stale_path, stale, reraise=True)

    report = _finalize_executor_forensics(
        PLAN_ID, plan_dir, _FakeProcess(-9, pid=4242)
    )

    assert report is not None
    assert report["pid"] == 4242
    assert report["source"] == "parent"

    files = _crash_files(plan_dir)
    assert len(files) == 2, "the parent did not write its own report"
    newest = json.loads(files[-1].read_text(encoding="utf-8"))
    assert newest["pid"] == 4242
    assert newest["source"] == "parent"


# ---------------------------------------------------------------------------
# 3. the flags
# ---------------------------------------------------------------------------


def test_parent_persists_flags_without_clobbering(tmp_path: Path) -> None:
    """The flags the row already had are still there after the crash.

    The flags column is shared: ``arch_enabled`` and its friends are
    written by other code paths.  The crash is **one more key** among
    them, not a replacement of them -- a write that dropped the others
    would lose the operator's switches.
    """
    db_path = _seed_execution_row(tmp_path, flags={"arch_enabled": True})
    plan_dir = tmp_path / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    report = _finalize_executor_forensics(PLAN_ID, plan_dir, _FakeProcess(-9))

    assert report is not None
    row = _read_row(db_path)
    assert row["flags"]["arch_enabled"] is True, (
        "the crash write dropped a flag another code path owns"
    )
    assert row["flags"]["crash"]["signal"] == 9
    assert row["flags"]["crash"]["killed_by"] == "signal"
    # The reason the run ended is the watcher's to write; this path
    # must not touch it.
    assert row["stop_reason"] == "startup_reconcile"


def test_parent_silent_on_clean_exit(tmp_path: Path) -> None:
    """rc=0 -> no file, no flags, nothing said about a run that ended well.

    A clean exit is not a crash.  Writing a report for it would fill
    the plan directory with documents about runs that worked, and the
    reader of "the newest report" would have to work out which of them
    were crashes.
    """
    db_path = _seed_execution_row(tmp_path, flags={"arch_enabled": True})
    plan_dir = tmp_path / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    report = _finalize_executor_forensics(PLAN_ID, plan_dir, _FakeProcess(0))

    assert report is None
    assert _crash_files(plan_dir) == []
    row = _read_row(db_path)
    assert row["flags"] == {"arch_enabled": True}, (
        "a clean exit changed the plan's flags"
    )


def test_parent_flags_merge_into_empty_flags(tmp_path: Path) -> None:
    """An empty flags column gains exactly one key: ``crash``.

    Not ``arch_enabled``, not ``test_enabled`` -- those belong to other
    code paths, and this one must not invent keys it does not own.
    """
    db_path = _seed_execution_row(tmp_path, flags={})
    plan_dir = tmp_path / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    _finalize_executor_forensics(PLAN_ID, plan_dir, _FakeProcess(-9))

    flags = _read_row(db_path)["flags"]
    assert set(flags) == {"crash"}
    assert set(flags["crash"]) == {
        "exit_code",
        "signal",
        "signal_name",
        "killed_by",
        "report",
        "crashed_at",
    }


def test_parent_report_path_is_relative_to_the_plan_dir(tmp_path: Path) -> None:
    """The path in the flags is relative -- a row is read on other machines.

    An absolute path would name this machine's directory layout, mean
    nothing to a reader on another one, and leak the operator's home
    directory into a shared row.
    """
    db_path = _seed_execution_row(tmp_path)
    plan_dir = tmp_path / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    _finalize_executor_forensics(PLAN_ID, plan_dir, _FakeProcess(-9))

    relpath = _read_row(db_path)["flags"]["crash"]["report"]
    assert isinstance(relpath, str)
    assert not Path(relpath).is_absolute()
    assert relpath.startswith("logs/executor_crash_")
    assert relpath.endswith(".json")


def test_persist_crash_flags_merges_without_clobbering(tmp_path: Path) -> None:
    """The flags write on its own merges too -- it does not replace."""
    db_path = _seed_execution_row(tmp_path, flags={"arch_enabled": True})
    plan_dir = tmp_path / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    _persist_crash_flags(
        PLAN_ID,
        {
            "exit_code": None,
            "signal": 9,
            "signal_name": "SIGKILL",
            "killed_by": "signal",
            "crashed_at": "2026-11-08T12:05:00+00:00",
        },
        plan_dir=plan_dir,
    )

    flags = _read_row(db_path)["flags"]
    assert flags["arch_enabled"] is True
    assert flags["crash"]["signal"] == 9


def test_parent_flags_merge_into_a_null_flags_column(tmp_path: Path) -> None:
    """A NULL flags column is an empty one: the merge ends with ``crash``.

    The column is NULL on a plan that has never been flagged.  Reading
    it as ``None`` and writing ``None`` back would leave the row
    unreadable for the next reader; reading it as ``{}`` and writing
    the crash into it gives the column a shape.
    """
    db_path = _seed_execution_row(tmp_path, flags_null=True)
    plan_dir = tmp_path / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    _finalize_executor_forensics(PLAN_ID, plan_dir, _FakeProcess(-9))

    flags = _read_row(db_path)["flags"]
    assert set(flags) == {"crash"}
    assert flags["crash"]["signal"] == 9


def test_persist_crash_flags_keeps_a_known_report_path(tmp_path: Path) -> None:
    """A write that cannot find the report keeps the path the row had.

    Replacing a known path with ``None`` would throw away the pointer
    to the evidence on the strength of a lookup that failed.
    """
    known = "logs/executor_crash_20201101T000000000000Z.json"
    db_path = _seed_execution_row(
        tmp_path, flags={"crash": {"report": known, "signal": 9}}
    )
    # A plan directory that holds no report at all.
    empty_dir = tmp_path / "elsewhere" / PLAN_ID
    empty_dir.mkdir(parents=True)

    _persist_crash_flags(
        PLAN_ID, {"signal": 9, "killed_by": "signal"}, plan_dir=empty_dir
    )

    flags = _read_row(db_path)["flags"]
    assert flags["crash"]["report"] == known
    assert flags["crash"]["signal"] == 9


# ---------------------------------------------------------------------------
# 4. the evidence itself
# ---------------------------------------------------------------------------


def test_parent_reads_last_events_from_the_execution_log(plan_dir: Path) -> None:
    """The events are the raw lines of ``execution.log``, and nothing else.

    A forensics document that paraphrases its own evidence is not
    evidence: the tail comes back as it sits on disk, and the task id
    is read out of the newest line that names one.
    """
    lines = [
        '{"event": "task_started", "task_id": "3-1"}',
        "the executor says something that is not json",
        '{"event": "task_started", "task_id": "3-2"}',
    ]
    (plan_dir / "execution.log").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    report = _finalize_executor_forensics(PLAN_ID, plan_dir, _FakeProcess(-9))

    assert report is not None
    assert report["last_events"] == lines
    assert report["active_task_id"] == "3-2"


# ---------------------------------------------------------------------------
# 5. the boundaries that must not change the run
# ---------------------------------------------------------------------------


def test_parent_survives_an_unwritable_plan_dir(plan_dir: Path) -> None:
    """A plan directory that cannot be written -> the run is unaffected.

    The caller is a process that has just watched a run die.  A report
    that cannot be written is a missing report, not a second failure.
    """
    logs = plan_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logs.chmod(0o555)
    try:
        report = _finalize_executor_forensics(
            PLAN_ID, plan_dir, _FakeProcess(-9)
        )
    finally:
        logs.chmod(0o755)

    assert report is None or isinstance(report, dict)
    # Nothing was raised: reaching this line is the assertion.


def test_parent_survives_an_unreachable_database(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database that cannot be opened -> the report is still written.

    The report is the evidence; the flags are a pointer at it.  Losing
    the pointer is bad, losing the evidence is worse.
    """

    def _unreachable(*args: Any, **kwargs: Any) -> Path:
        raise RuntimeError("the state database is not there")

    monkeypatch.setattr(server, "_state_db_path", _unreachable)

    report = _finalize_executor_forensics(PLAN_ID, plan_dir, _FakeProcess(-9))

    assert report is not None
    assert len(_crash_files(plan_dir)) == 1


# ---------------------------------------------------------------------------
# 6. the wiring — the real watcher, end to end
# ---------------------------------------------------------------------------


def _drive_the_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, returncode: int
) -> tuple[Path, List[str]]:
    """Run the real watcher body to completion; return the plan dir + the calls.

    The watcher is a daemon thread in production; the test hands the
    body over instead (``_run_in_plan_ctx``), so the assertions cannot
    race the thread.

    The returned list is the order in which the database was written
    to, which is how the "the flags are written before the status" rule
    is pinned.
    """
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "1", "title": "t", "depends_on": []}]}),
        encoding="utf-8",
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    db_path = _seed_execution_row(tmp_path)

    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    monkeypatch.setattr(server, "_reject_if_placeholder_prd", lambda *a, **k: None)
    monkeypatch.setattr(
        "state_machine.db.archive_scan.classify_plan", lambda *a, **k: "active",
    )
    monkeypatch.setattr(
        server,
        "_spawn_executor_subprocess",
        lambda **kwargs: (_FakeProcess(returncode), plan_dir / "executor.log"),
    )
    monkeypatch.setattr(server, "_refresh_usage_report", lambda *a, **k: None)
    monkeypatch.setattr(
        server,
        "_run_auto_verification_loop",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        server, "_settle_phase_after_verification_loop", lambda *a, **k: None,
    )

    class _FakePlanState:
        """Stands in for ``server.PlanState`` — the name the route uses."""

        def __init__(self, plan_dir_arg):
            self.plan_dir = plan_dir_arg

        def transition_to(self, phase):
            pass

        def force_set_phase(self, phase):
            pass

    monkeypatch.setattr(server, "PlanState", _FakePlanState)

    # The order of the two writes is part of the contract, so it is
    # recorded rather than inferred.
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    calls: List[str] = []
    original_flags = ExecutionRepository.update_flags
    original_status = ExecutionRepository.update_status

    def _tracking_flags(self, plan_id_arg, flags):
        calls.append("update_flags")
        return original_flags(self, plan_id_arg, flags)

    def _tracking_status(self, plan_id_arg, status, stop_reason=None):
        calls.append("update_status")
        return original_status(self, plan_id_arg, status, stop_reason)

    monkeypatch.setattr(ExecutionRepository, "update_flags", _tracking_flags)
    monkeypatch.setattr(ExecutionRepository, "update_status", _tracking_status)

    captured: List[Any] = []
    monkeypatch.setattr(
        server, "_run_in_plan_ctx", lambda plan_id_arg, fn: captured.append(fn),
    )

    server._execution_state.pop(PLAN_ID, None)
    try:
        server.start_execution(
            PLAN_ID,
            server.StartExecutionRequest(project_dir=str(project_dir)),
        )
        deadline = time.monotonic() + 10.0
        while not captured and time.monotonic() < deadline:
            time.sleep(0.01)
        assert captured, "the execution watcher never handed over"
        captured[0]()
    finally:
        server._execution_state.pop(PLAN_ID, None)

    return plan_dir, calls


def test_watcher_writes_the_crash_report_for_a_killed_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor is SIGKILLed -> the watcher leaves the scene behind.

    This is the wiring, end to end: the real ``start_execution`` →
    watcher → database path, with only the executor process itself
    stood in for.  Without it, the parent could write the fallback
    perfectly well and nobody would ever call it.
    """
    plan_dir, calls = _drive_the_watcher(tmp_path, monkeypatch, returncode=-9)

    files = _crash_files(plan_dir)
    assert len(files) == 1, "the watcher left no crash report behind"
    report = json.loads(files[0].read_text(encoding="utf-8"))
    assert report["signal"] == 9
    assert report["signal_name"] == "SIGKILL"
    assert report["killed_by"] == "signal"
    assert report["source"] == "parent"
    assert report["plan_id"] == PLAN_ID

    row = _read_row(tmp_path / "state.db")
    assert row["flags"]["crash"]["signal"] == 9
    # The reason the run ended is the watcher's own: the wording is
    # decided by the watcher's `_crash_stop_reason` (`signal:9` for a
    # SIGKILL), and the fallback scene write does not put its own words
    # in that column.
    assert row["stop_reason"] == "signal:9"


def test_watcher_writes_the_flags_before_the_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flags are written before the watcher's final status write.

    The status write is the last thing the watcher does.  If the flags
    were written after it, a crash between the two would leave a row
    that says "failed" with nothing pointing at the scene of the death
    — the row would be the answer to a question nobody asked.
    """
    _plan_dir, calls = _drive_the_watcher(tmp_path, monkeypatch, returncode=-9)

    assert "update_flags" in calls
    assert "update_status" in calls
    assert calls.index("update_flags") < calls.index("update_status")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
