"""2026-10-24: the ``waiting_for_quota`` disposition when a run ends.

Background
----------
When every provider runs out of capacity, the executor parks the plan
instead of failing its tasks: it writes ``stop_reason="waiting_for_quota"``
and a ``reason_detail`` blob into the top level of ``tasks.json`` and
leaves every task eligible for another dispatch (see
``agent._stop_reason_for_quota_wait``).

The server side had no disposition for that marker.  A run that ended
with unfinished tasks was judged "failed" — or, when every unfinished
task was blocked by a failed upstream, "skipped + auto-verified on the
completed subset".  Both are wrong for a quota park:

* "failed" strands every task that was only waiting for capacity to
  come back, and it is a lie: nothing failed.
* "skipped" throws away the downstream work and then verifies the plan
  as if it were done.

So a plan carrying the marker must be parked in ``waiting_for_quota`` —
alive, restartable, with every task untouched.

Contract pinned here
--------------------
* ``_quota_wait_detail`` reads the marker; no marker → ``None``; a
  broken ``reason_detail`` → an empty dict (the marker still counts).
* ``_enter_quota_wait`` parks the plan (execution status + workflow
  phase) and returns ``True``.
* The disposition is taken BEFORE the blocked-only branch: no task is
  skipped, the auto-verification loop does not run, and ``failed`` is
  never written.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` must be imported first: it seeds ``sys.modules['server']``
# and then wires the ``routes`` package, and ``routes.execution``
# late-binds back into it.
import server  # noqa: E402
from routes import execution as routes_execution  # noqa: E402


#: The marker the executor writes — the exact shape task 4 produces.
_MARKER = {
    "providers": ["MiniMax"],
    "needs_manual_topup": False,
    "waited_sec": 3.5,
    "at": "2026-10-24T10:00:00",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state_db(tmp_path: Path) -> Path:
    """Allocate a hermetic ``state.db`` and return its path."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()
    return db_path


def _seed_plan(
    db_path: Path,
    plan_id: str,
    project_dir: Path,
    phase: str = "queued",
) -> None:
    """Insert the ``plan_routing`` / ``plan_execution`` rows a start needs.

    2026-10-26 (merge): ``queued``, not ``ready`` — ``ready`` is the legacy
    alias of ``queued`` (R10) and the execution-start CAS refuses it on
    purpose; ``queued`` is the phase the startup migration leaves behind.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, substage, current_phase, version, completed_phases, "
            " review_rounds, flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, None, phase, 0, "[]",
                "{}", "{}",
                json.dumps(
                    {
                        "status": "pending",
                        "round": 0,
                        "max_rounds": 3,
                        "stop_reason": None,
                    }
                ),
                "2026-10-24T00:00:00", "2026-10-24T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution "
            "(plan_id, attempt_count, project_dir, stop_reason, task_progress, "
            " next_run_at, card_state, flags, exec_pid, exec_status, "
            " started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, 0, str(project_dir), None, json.dumps({"tasks": {}}),
                None, "{}", "{}", None, "not_started", None,
                "2026-10-24T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


class _FakeProcess:
    """Minimal stand-in for the executor ``subprocess.Popen`` handle."""

    def __init__(self) -> None:
        self.pid = 424242
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode


def _write_tasks(
    plan_dir: Path,
    *,
    stop_reason: str = None,
    reason_detail: str = None,
) -> Path:
    """Write a ``tasks.json`` that optionally carries the quota marker."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    payload = {"tasks": [{"id": "1", "title": "t", "depends_on": []}]}
    if stop_reason is not None:
        payload["stop_reason"] = stop_reason
    if reason_detail is not None:
        payload["reason_detail"] = reason_detail
    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text(json.dumps(payload), encoding="utf-8")
    return tasks_file


def _read_log_entries(log_file: Path) -> list[dict]:
    """Parse the plan's structured execution log into JSON entries."""
    if not log_file.exists():
        return []
    entries = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


# ---------------------------------------------------------------------------
# 1. Reading the marker
# ---------------------------------------------------------------------------


def test_quota_wait_detail_reads_marker(tmp_path):
    """The marker's ``reason_detail`` comes back normalised."""
    plan_dir = tmp_path / "plans" / "p1"
    tasks_file = _write_tasks(
        plan_dir,
        stop_reason="waiting_for_quota",
        reason_detail=json.dumps(_MARKER),
    )

    detail = routes_execution._quota_wait_detail(tasks_file)

    assert detail == {
        "providers": ["MiniMax"],
        "needs_manual_topup": False,
        "waited_sec": 3.5,
        "at": "2026-10-24T10:00:00",
    }


def test_missing_marker_returns_none(tmp_path):
    """No marker, a different marker, and an unreadable plan are all None."""
    plan_dir = tmp_path / "plans" / "p2"

    plain = _write_tasks(plan_dir)
    assert routes_execution._quota_wait_detail(plain) is None

    other = _write_tasks(
        plan_dir,
        stop_reason="success",
        reason_detail=json.dumps(_MARKER),
    )
    assert routes_execution._quota_wait_detail(other) is None

    assert routes_execution._quota_wait_detail(tmp_path / "no-such.json") is None


def test_non_json_detail_is_empty_dict(tmp_path):
    """A broken ``reason_detail`` costs the detail, not the marker."""
    plan_dir = tmp_path / "plans" / "p3"
    tasks_file = _write_tasks(
        plan_dir,
        stop_reason="waiting_for_quota",
        reason_detail="this is not json",
    )

    assert routes_execution._quota_wait_detail(tasks_file) == {}


# ---------------------------------------------------------------------------
# 2. Parking the plan
# ---------------------------------------------------------------------------


def test_enter_quota_wait_sets_phase_and_status(tmp_path, monkeypatch):
    """The plan ends up in ``waiting_for_quota`` — in the DB and on disk."""
    plan_id = "quota-phase-plan"
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / plan_id
    tasks_file = _write_tasks(
        plan_dir,
        stop_reason="waiting_for_quota",
        reason_detail=json.dumps(_MARKER),
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    db_path = _make_state_db(tmp_path)
    _seed_plan(db_path, plan_id, project_dir, phase="executing")
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)

    state = {"status": "running", "stop_reason": None, "ended_at": "never"}

    assert routes_execution._enter_quota_wait(
        plan_id, plan_dir, state, tasks_file,
    ) is True

    # In-memory state — what the status endpoint answers with.
    assert state["status"] == "waiting_for_quota"
    assert state["stop_reason"] == "waiting_for_quota"
    assert state["ended_at"] is None

    # The workflow phase.
    from plan_state import PlanState

    assert PlanState(plan_dir).get_current_phase() == "waiting_for_quota"

    # The execution status row.
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(db_path)
    try:
        summary = ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()
    assert summary["exec_status"] == "waiting_for_quota"

    # And the event says why, with the marker's detail attached.
    entries = _read_log_entries(plans_dir / plan_id / "execution.log")
    waiting = [e for e in entries if e.get("event") == "execution_waiting_for_quota"]
    assert waiting, f"no execution_waiting_for_quota event in {entries}"
    assert waiting[0]["data"]["providers"] == ["MiniMax"]


def test_phase_transition_falls_back_to_force_set(tmp_path, monkeypatch):
    """An illegal transition is forced — and the force is announced."""
    plan_id = "quota-force-plan"
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / plan_id
    tasks_file = _write_tasks(
        plan_dir,
        stop_reason="waiting_for_quota",
        reason_detail=json.dumps(_MARKER),
    )

    db_path = _make_state_db(tmp_path)
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)

    forced: list[str] = []

    class _FakePlanState:
        """Stands in for ``server.PlanState`` — the name the route uses."""

        def __init__(self, plan_dir):
            self.plan_dir = plan_dir

        def transition_to(self, phase):
            raise ValueError(
                f"Illegal transition from 'queued' to '{phase}'",
            )

        def force_set_phase(self, phase):
            forced.append(phase)

    monkeypatch.setattr(server, "PlanState", _FakePlanState)

    state = {"status": "running", "stop_reason": None, "ended_at": None}

    assert routes_execution._enter_quota_wait(
        plan_id, plan_dir, state, tasks_file,
    ) is True

    assert forced == ["waiting_for_quota"]
    assert state["status"] == "waiting_for_quota"

    entries = _read_log_entries(plans_dir / plan_id / "execution.log")
    events = [e.get("event") for e in entries]
    assert "execution_quota_wait_phase_forced" in events


# ---------------------------------------------------------------------------
# 3. The disposition at the end of a run
# ---------------------------------------------------------------------------


def test_returning_true_skips_failed_and_verification(tmp_path, monkeypatch):
    """A parked plan is not failed, not skipped and not auto-verified.

    This drives the real watcher body: the executor "exits 0" with one
    unfinished task, and the marker says the run parked for quota.
    """
    plan_id = "quota-e2e-plan"
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / plan_id
    tasks_file = _write_tasks(
        plan_dir,
        stop_reason="waiting_for_quota",
        reason_detail=json.dumps(_MARKER),
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    db_path = _make_state_db(tmp_path)
    _seed_plan(db_path, plan_id, project_dir, phase="queued")

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
        "_count_unfinished_tasks",
        lambda *a, **k: {
            "total": 1,
            "pending_or_unstarted": 1,
            "failed": 0,
            "unfinished_ids": ["1"],
        },
    )
    monkeypatch.setattr(
        server,
        "_spawn_executor_subprocess",
        lambda **kwargs: (_FakeProcess(), tmp_path / "executor.log"),
    )

    verification_calls: list[tuple] = []
    skipped_calls: list[tuple] = []
    monkeypatch.setattr(
        server,
        "_run_auto_verification_loop",
        lambda *a, **k: verification_calls.append((a, k)),
    )
    monkeypatch.setattr(
        server,
        "_mark_task_skipped",
        lambda *a, **k: skipped_calls.append((a, k)),
    )

    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    status_calls: list[tuple] = []

    def _record_status(self, plan_id_arg, status):
        status_calls.append((plan_id_arg, status))

    monkeypatch.setattr(ExecutionRepository, "update_status", _record_status)

    # Hand the watcher body over instead of letting a daemon thread run
    # it: the assertions below must not race the thread.
    captured: list = []
    monkeypatch.setattr(
        server, "_run_in_plan_ctx", lambda plan_id_arg, fn: captured.append(fn),
    )

    server._execution_state.pop(plan_id, None)
    try:
        server.start_execution(
            plan_id,
            server.StartExecutionRequest(project_dir=str(project_dir)),
        )
        deadline = time.monotonic() + 10.0
        while not captured and time.monotonic() < deadline:
            time.sleep(0.01)
        assert captured, "the execution watcher never handed over"
        captured[0]()
        state = server._execution_state[plan_id]
    finally:
        server._execution_state.pop(plan_id, None)

    # Parked, not failed.
    assert state["status"] == "waiting_for_quota"
    assert state["stop_reason"] == "waiting_for_quota"
    assert state["ended_at"] is None

    # The blocked-only branch and the failed branch were both skipped.
    assert verification_calls == []
    assert skipped_calls == []
    assert [s for _pid, s in status_calls if s == "failed"] == []
    assert (plan_id, "waiting_for_quota") in status_calls

    # No task status was touched.
    tasks = json.loads(tasks_file.read_text(encoding="utf-8"))
    assert all(
        task.get("status") in (None, "pending", "not_started")
        for task in tasks["tasks"]
    ), tasks["tasks"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
