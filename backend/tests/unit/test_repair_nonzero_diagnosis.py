"""What a finished repair batch actually ENDED AS.

Background
----------
``_run_repair_execution_async`` used to make TWO independent judgements
and write two unrelated reason strings:

* the executor exited non-zero → ``repair_non_zero_exit``;
* the executor exited 0 but repair tasks are still queued →
  ``repair_exited_with_unfinished_tasks:N``.

The two facts are independent — a batch can be non-zero AND still have
work queued — and neither string could be read as the other.  A caller
therefore could not tell "there is still work to run" (retry) from "the
gate is red and re-running buys nothing" (stop).  The two need opposite
responses.

Both branches now go through one classifier
(:func:`verification.repair_outcome.classify_repair_outcome`), and the
verdict it produces is what lands in ``_execution_state``.

Contract pinned here
--------------------
* exit code 0 with one task still queued → ``repair_batch_not_drained:
  1_pending`` — the work was LEFT, and the count is part of the reason.
* exit code 3 with a failed repair row → ``repair_task_failed`` — the
  repair task itself broke.
* exit code 3 with every row completed → ``repair_gate_red`` — the
  repair worked, the thing it repaired is still wrong.
* rows that cannot be read at all → ``repair_non_zero_exit``: no
  classified verdict is supportable, so the unclassified reason is the
  honest answer — and the round must not raise.
* a signal death (exit code -15) does NOT change any of the above:
  the ending of the BATCH is the classifier's verdict, verbatim, and
  the death of the PROCESS is recorded beside it in ``repair_death``.
  A death must never overwrite the outcome, because the outcome is
  what tells the chain whether there is work left to run.
"""

from __future__ import annotations

import json
import sqlite3
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
        "updated_at": "2026-11-01T00:00:00",
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
    # NOTE: ``state_machine.db.connection.open`` is deliberately NOT
    # stubbed here — the count of pending / failed rows is what the
    # verdict is made of, so the real overlay has to be read.
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
# The three verdicts
# ---------------------------------------------------------------------------


def test_exit_zero_with_pending_writes_batch_not_drained(
    monkeypatch, tmp_path, db_path,
):
    """rc==0 with work still queued → the batch did NOT drain.

    The executor believes it finished (exit code 0), but the runtime
    overlay says a repair task never ran.  The verdict is
    ``repair_batch_not_drained`` and the count travels with it — one
    pending is a different situation from two.
    """
    disk = [{"id": "1"}, {"id": "repair-r1-01"}]
    runtime = [
        _repair_row("1", "completed", None, None),
        _repair_row("repair-r1-01", "pending", "VP-005", 1),
    ]
    _seed_plan(tmp_path, "p-nz-pending", disk, runtime, db_path)

    state = _run_repair(
        monkeypatch, tmp_path, "p-nz-pending", db_path, returncode=0,
    )

    assert state["stop_reason"] == "repair_batch_not_drained:1_pending"
    assert state["status"] == "failed", (
        "a round whose repair tasks never ran must not be recorded as "
        "completed"
    )
    assert state["repair_unfinished_tasks"] == 1


def test_nonzero_with_failed_repair_row_says_task_failed(
    monkeypatch, tmp_path, db_path,
):
    """rc!=0 with a failed repair row → the REPAIR broke.

    Not the gate: the repair task itself failed, so the batch is over
    and it is broken.  This is the case the old bare
    ``repair_non_zero_exit`` could not express.
    """
    disk = [{"id": "1"}, {"id": "repair-r1-01"}]
    runtime = [
        _repair_row("1", "completed", None, None),
        _repair_row("repair-r1-01", "failed", "VP-005", 1),
    ]
    _seed_plan(tmp_path, "p-nz-failed", disk, runtime, db_path)

    state = _run_repair(
        monkeypatch, tmp_path, "p-nz-failed", db_path, returncode=3,
    )

    assert state["stop_reason"] == "repair_task_failed"
    assert state["status"] == "failed"


def test_nonzero_without_failed_rows_says_gate_red(
    monkeypatch, tmp_path, db_path,
):
    """rc!=0 with nothing failed → the GATE is red.

    The repair tasks all ran and none of them broke, so what is still
    wrong is the thing they were repairing.  Re-running the same repair
    buys nothing — the caller has to know that.
    """
    disk = [{"id": "1"}, {"id": "repair-r1-01"}]
    runtime = [
        _repair_row("1", "completed", None, None),
        _repair_row("repair-r1-01", "completed", "VP-005", 1),
    ]
    _seed_plan(tmp_path, "p-nz-gate", disk, runtime, db_path)

    state = _run_repair(
        monkeypatch, tmp_path, "p-nz-gate", db_path, returncode=3,
    )

    assert state["stop_reason"] == "repair_gate_red"
    assert state["status"] == "failed"


def test_unreadable_rows_fall_back_to_non_zero_exit(
    monkeypatch, tmp_path, db_path,
):
    """Rows that cannot be read → the unclassified reason, and no raise.

    "Looked and found nothing" and "could not look" are different
    verdicts, and only the first one supports a claim about what the
    batch did.  When the overlay cannot be read there is no classified
    verdict to report, so the repair path falls back to the plain
    ``repair_non_zero_exit`` — and, above all, does not take the round
    down with it.
    """
    disk = [{"id": "1"}, {"id": "repair-r1-01"}]
    runtime = [_repair_row("1", "completed", None, None)]
    _seed_plan(tmp_path, "p-nz-unreadable", disk, runtime, db_path)

    import state_machine.db.connection as db_connection

    def _boom(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db_connection, "open", _boom)

    state = _run_repair(
        monkeypatch, tmp_path, "p-nz-unreadable", db_path, returncode=3,
    )

    assert state["stop_reason"] == "repair_non_zero_exit"
    assert state["status"] == "failed"


def test_signal_death_keeps_the_work_left_and_records_the_death(
    monkeypatch, tmp_path, db_path,
):
    """A killing must not hide the work that is still queued.

    A signal death is a fact about the PROCESS; the batch's ending is a
    fact about the BATCH.  When the death's own wording overwrote the
    outcome's, "there is still work to run" became invisible: the
    redispatch that reads ``repair_batch_not_drained:`` never saw it,
    so a pending repair task ended the verification chain instead of
    being the reason to run the next round.

    So the outcome is the classifier's, verbatim, and the death is
    recorded beside it — in its own field, where it says what it
    actually says: the executor was killed.
    """
    disk = [{"id": "1"}, {"id": "repair-r1-01"}]
    runtime = [
        _repair_row("1", "completed", None, None),
        _repair_row("repair-r1-01", "pending", "VP-005", 1),
    ]
    _seed_plan(tmp_path, "p-nz-signal-pending", disk, runtime, db_path)

    state = _run_repair(
        monkeypatch, tmp_path, "p-nz-signal-pending", db_path, returncode=-15,
    )

    # The OUTCOME — the classifier's, word for word.
    assert state["stop_reason"] == "repair_batch_not_drained:1_pending", state
    assert state["repair_unfinished_tasks"] == 1
    assert state["should_continue"] is True, (
        "work was left, so the next round is the reason to continue"
    )
    assert state["repair_failed"] is False
    assert state["status"] == "failed"
    # The DEATH — recorded beside the outcome, not on top of it.
    assert state["repair_death"] == "repair_signal:15", state


def test_signal_death_with_a_failed_repair_row_keeps_the_failure(
    monkeypatch, tmp_path, db_path,
):
    """A killing must not hide a repair task that broke either.

    The other side of the same door: "the repair task itself failed"
    and "the executor was killed" are independent facts, and the one
    that decides what happens next is the failure.  A death that
    overwrote it would report a killing where the operator has to act
    on a broken repair.
    """
    disk = [{"id": "1"}, {"id": "repair-r1-01"}]
    runtime = [
        _repair_row("1", "completed", None, None),
        _repair_row("repair-r1-01", "failed", "VP-005", 1),
    ]
    _seed_plan(tmp_path, "p-nz-signal-failed", disk, runtime, db_path)

    state = _run_repair(
        monkeypatch, tmp_path, "p-nz-signal-failed", db_path, returncode=-15,
    )

    assert state["stop_reason"] == "repair_task_failed", state
    assert state["repair_failed"] is True
    assert state["status"] == "failed"
    assert state["repair_death"] == "repair_signal:15", state
