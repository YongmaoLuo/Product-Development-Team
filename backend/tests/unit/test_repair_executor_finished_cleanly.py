"""The repair path must not claim a clean exit when the executor failed.

``_run_repair_execution_async._run`` used to write
``state["executor_finished_cleanly"] = True`` unconditionally, right
after ``process.wait()`` returned — including for a NON-ZERO return
code.

That flag is the executor watchdog's only "the success path is already
in flight, don't fire ``_mark_failed_dead``" signal
(:func:`_lazy_check_execution`). Setting it for a crashed executor
tells the watchdog to stand down, so the crash is never accounted for:
the repair round dies with a non-zero exit and nothing notices.

So: the flag means "the subprocess exited 0". A non-zero exit must
leave it False, so the watchdog can still backstop the round.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import server


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _mock_subprocess(returncode: int = 0, pid: int = 424242):
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = pid
    proc.wait = MagicMock(return_value=returncode)
    return proc


def _seed_plan(tmp_path, plan_id, disk_tasks, runtime_rows, db_path):
    """Write ``tasks.json`` and the matching ``plan_tasks`` rows."""
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": disk_tasks}), encoding="utf-8",
    )

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        for row in runtime_rows:
            row = {**row, "plan_id": plan_id}
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            conn.execute(
                f"INSERT INTO plan_tasks ({cols}) VALUES ({marks})",
                tuple(row.values()),
            )
        conn.commit()
    finally:
        conn.close()
    return plan_dir


def _repair_row(task_id, status, failed_vp_id, round_n, plan_id=""):
    return {
        "plan_id": plan_id, "task_id": task_id, "status": status,
        "task_group": f"repair-round-{round_n}" if round_n else None,
        "failed_vp_id": failed_vp_id, "round": round_n,
        "updated_at": "2026-10-23T00:00:00",
    }


def _run_repair(monkeypatch, tmp_path, plan_id, db_path, returncode=0):
    """Drive ``_run_repair_execution_async`` to completion; return state."""
    monkeypatch.setattr(
        server, "_state_db_path", lambda *_a, **_kw: Path(db_path),
    )
    monkeypatch.setattr(
        server, "_plan_dir",
        lambda pid: tmp_path / "plans" / pid,
    )
    monkeypatch.setattr(
        server, "_spawn_executor_subprocess",
        lambda **kw: (_mock_subprocess(returncode=returncode), tmp_path / "log"),
    )
    monkeypatch.setattr(
        server, "PlanState",
        lambda *a, **kw: MagicMock(force_set_phase=MagicMock()),
    )
    with server._execution_state_lock:
        server._execution_state.clear()

    done = threading.Event()
    seen = {}

    def _cb(rc):
        seen["rc"] = rc
        done.set()

    server._run_repair_execution_async(
        plan_id, tmp_path, on_complete=_cb,
    )
    assert done.wait(timeout=5.0), "on_complete never fired"
    with server._execution_state_lock:
        return dict(server._execution_state[plan_id])


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state.db"


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_non_zero_exit_is_not_marked_clean(monkeypatch, tmp_path, db_path):
    """A crashed executor must NOT look like a clean finish.

    ``executor_finished_cleanly=True`` makes
    :func:`_lazy_check_execution` early-return instead of firing
    ``_mark_failed_dead``. On a non-zero exit that is the watchdog
    standing down over a real crash.
    """
    disk = [{"id": "1"}]
    runtime = [_repair_row("1", "completed", None, None)]
    _seed_plan(tmp_path, "p-clean-rc", disk, runtime, db_path)

    state = _run_repair(
        monkeypatch, tmp_path, "p-clean-rc", db_path, returncode=3,
    )

    assert state["executor_finished_cleanly"] is False, (
        "a non-zero exit was reported as a clean finish — the executor "
        "watchdog now yields instead of backing the round up"
    )
    assert state["executor_returncode"] == 3
    assert state["status"] == "failed"
    assert state["stop_reason"] == "repair_non_zero_exit"


def test_zero_exit_is_still_marked_clean(monkeypatch, tmp_path, db_path):
    """rc==0 keeps the watchdog-yielding flag — that is its whole point.

    Without it, the watchdog's 30s tick would race the success path
    between ``process.wait()`` returning and ``state["status"]`` being
    written, and mark a healthy round as dead.
    """
    disk = [{"id": "1"}, {"id": "repair-r1-01"}]
    runtime = [
        _repair_row("1", "completed", None, None),
        _repair_row("repair-r1-01", "completed", "VP-005", 1),
    ]
    _seed_plan(tmp_path, "p-clean-ok", disk, runtime, db_path)

    state = _run_repair(
        monkeypatch, tmp_path, "p-clean-ok", db_path, returncode=0,
    )

    assert state["executor_finished_cleanly"] is True
    assert state["executor_returncode"] == 0
    assert state["status"] == "completed"
    assert state["stop_reason"] is None
