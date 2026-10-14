"""W4 + W5 (2026-10-11): honest accounting on the repair path.

W4 — a 0 return code is not a completed repair round
----------------------------------------------------
``agent.py`` hits ``No schedulable micro-layer found`` and simply
``break``s — no exception, no non-zero exit. ``_run_repair_execution_async``
used to translate any rc==0 into ``status="completed"`` /
``stop_reason=None``, so a round could be persisted as
``exec_status='completed'`` with ``plan_execution.stop_reason IS NULL``
while most of its repair tasks sat ``pending`` and would never run.

The main execution path has always cross-checked the runtime overlay via
``_count_unfinished_tasks``; that helper had exactly one caller and the
repair path was not it. This file pins that it now is.

The ⚠️ below is the load-bearing assertion: recording the failure must
NOT copy the main path's ``_mark_task_skipped(..., blocked_by_failed_upstream)``.
``skipped`` is a terminal *success* under ``agent.is_dependency_ready`` and
unlocks downstream, so marking a task "stuck behind a failed upstream" as
skipped would start that task's dependents with no upstream output.

W5 — cross-round supersede
---------------------------
The same ``failed_vp_id`` was re-queued every round: 34 rows over 4 rounds
covering only 10 distinct VPs.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        "updated_at": "2026-10-11T00:00:00",
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
    # stubbed here. ``_count_unfinished_tasks`` — the thing under test —
    # opens the same connection, and stubbing it would make every task
    # look "never started" and mask the real distinction. The
    # ``plan_execution`` persistence that follows finds no row and is
    # swallowed by its own best-effort ``except``.
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
# W4 — stop_reason must not be None when work was left behind
# ---------------------------------------------------------------------------


def test_repair_exit_zero_with_unfinished_tasks_is_not_recorded_completed(
    monkeypatch, tmp_path, db_path,
):
    """The measured production failure, reproduced.

    The executor bails out at the first micro-layer, exits 0, and 30 of
    34 repair tasks stay ``pending``. That must NOT become
    ``status="completed"`` / ``stop_reason=None``.
    """
    # NOTE: the repair ids appear in ``tasks.json`` as well as in
    # ``plan_tasks``: that is the production shape, because the
    # executor's load-time
    # reconcile injects state.db orphans into the DAG.
    disk = [
        {"id": "1"}, {"id": "2"},
        {"id": "repair-r1-01"}, {"id": "repair-r1-02"},
        {"id": "repair-r1-03"},
    ]
    runtime = [
        _repair_row("1", "completed", None, None),
        _repair_row("2", "completed", None, None),
        # Round 1 chain head ran and failed…
        _repair_row("repair-r1-01", "failed", "VP-005", 1),
        # …so everything behind it never became schedulable.
        _repair_row("repair-r1-02", "pending", "VP-006", 1),
        _repair_row("repair-r1-03", "pending", "VP-008", 1),
    ]
    _seed_plan(tmp_path, "p-w4", disk, runtime, db_path)

    state = _run_repair(monkeypatch, tmp_path, "p-w4", db_path, returncode=0)

    assert state["stop_reason"] == "repair_exited_with_unfinished_tasks:2", (
        "rc==0 was accepted as completion while 2 repair tasks never ran — "
        "this is exactly the 'exec_status=completed, stop_reason NULL' "
        "state the guard exists to prevent"
    )
    assert state["status"] == "failed"
    assert state["repair_unfinished_tasks"] == 2
    assert state["executor_finished_cleanly"] is True


def test_repair_exit_zero_with_everything_drained_still_completes(
    monkeypatch, tmp_path, db_path,
):
    """The gate must not fire on a genuinely finished round."""
    disk = [
        {"id": "1"}, {"id": "2"}, {"id": "repair-r1-01"},
        {"id": "repair-r1-02"},
    ]
    runtime = [
        _repair_row("1", "completed", None, None),
        _repair_row("2", "completed", None, None),
        _repair_row("repair-r1-01", "completed", "VP-005", 1),
        _repair_row("repair-r1-02", "completed", "VP-006", 1),
    ]
    _seed_plan(tmp_path, "p-w4-ok", disk, runtime, db_path)

    state = _run_repair(monkeypatch, tmp_path, "p-w4-ok", db_path, returncode=0)
    assert state["status"] == "completed"
    assert state["stop_reason"] is None


def test_failed_tasks_alone_do_not_trigger_the_unfinished_gate(
    monkeypatch, tmp_path, db_path,
):
    """A ``failed`` task is not "unfinished" — only pending/never-started is.

    ``_count_unfinished_tasks`` buckets ``failed`` separately. A round whose
    only non-success row is a genuinely failed task (nothing else to run)
    must still complete, or every legitimately-failing repair round would
    now report a stop reason.
    """
    disk = [{"id": "1"}, {"id": "repair-r1-01"}]
    runtime = [
        _repair_row("1", "completed", None, None),
        _repair_row("repair-r1-01", "failed", "VP-005", 1),
    ]
    _seed_plan(tmp_path, "p-w4-f", disk, runtime, db_path)

    state = _run_repair(monkeypatch, tmp_path, "p-w4-f", db_path, returncode=0)
    assert state["status"] == "completed"
    assert state["stop_reason"] is None


def test_non_zero_exit_still_reports_repair_non_zero_exit(
    monkeypatch, tmp_path, db_path,
):
    disk = [{"id": "1"}]
    runtime = [_repair_row("1", "completed", None, None)]
    _seed_plan(tmp_path, "p-w4-rc", disk, runtime, db_path)

    state = _run_repair(
        monkeypatch, tmp_path, "p-w4-rc", db_path, returncode=3,
    )
    assert state["status"] == "failed"
    assert state["stop_reason"] == "repair_non_zero_exit"


# ---------------------------------------------------------------------------
# W4 ⚠️ — the repair path must NOT mark blocked rows ``skipped``
# ---------------------------------------------------------------------------


def test_blocked_repair_rows_stay_pending_never_skipped(
    monkeypatch, tmp_path, db_path,
):
    """The line that must never be copied from the main path.

    ``routes/execution.py`` marks tasks ``skipped`` with reason
    ``blocked_by_failed_upstream`` when every unfinished task is behind a
    failed upstream. Under ``agent.is_dependency_ready``, ``skipped`` is a
    terminal SUCCESS and unlocks downstream — so doing that here would
    start dependents with no upstream output.

    The repair path writes only ``stop_reason``; row statuses are left
    exactly as the executor left them.
    """
    disk = [
        {"id": "1"}, {"id": "repair-r1-01"}, {"id": "repair-r1-02"},
    ]
    runtime = [
        _repair_row("1", "completed", None, None),
        _repair_row("repair-r1-01", "failed", "VP-005", 1),
        _repair_row("repair-r1-02", "pending", "VP-006", 1),
    ]
    plan_dir = _seed_plan(tmp_path, "p-w4-blocked", disk, runtime, db_path)

    _run_repair(monkeypatch, tmp_path, "p-w4-blocked", db_path, returncode=0)

    from state_machine.db.connection import open as open_db
    conn = open_db(db_path)
    try:
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT task_id, status, failure_reason FROM plan_tasks "
                "WHERE plan_id = ?", ("p-w4-blocked",),
            ).fetchall()
        }
    finally:
        conn.close()

    assert rows["repair-r1-02"][0] == "pending", (
        "the blocked task's status was mutated — it must keep its real "
        "state; only stop_reason is written"
    )
    assert rows["repair-r1-02"][1] is None, (
        "a blocked_by_failed_upstream-style reason was stamped — that "
        "reason travels with the skipped marking and must not be reused"
    )
    assert rows["repair-r1-01"][0] == "failed"
    assert "skipped" not in {s for s, _ in rows.values()}, (
        "the repair path marked something skipped — under the readiness "
        "contract that UNLOCKS downstream behind a failed upstream"
    )


def test_skipped_never_unlocks_a_downstream_behind_a_failed_upstream():
    """The contract itself, spelled out.

    Not a test of new code — a pin on ``agent.is_dependency_ready`` so
    nobody "fixes" the repair path later by treating ``skipped`` as
    merely cosmetic. ``failed`` must block; ``skipped`` must unlock.
    """
    from agent import is_dependency_ready

    class _T:
        def __init__(self, tid, status, depends_on=()):
            self.id = tid
            self.status = status
            self.depends_on = list(depends_on)

    downstream = lambda: _T("down", "pending", depends_on=["up"])

    assert is_dependency_ready(downstream(), [_T("up", "failed")])[0] is False
    assert is_dependency_ready(downstream(), [_T("up", "pending")])[0] is False
    assert is_dependency_ready(downstream(), [_T("up", "in_progress")])[0] is False
    # …while the two terminal-success statuses both unlock.
    assert is_dependency_ready(downstream(), [_T("up", "skipped")])[0] is True
    assert is_dependency_ready(downstream(), [_T("up", "completed")])[0] is True


# ---------------------------------------------------------------------------
# W5 — cross-round supersede
# ---------------------------------------------------------------------------


def _supersede(plan_id, round_n, vp_ids):
    from verification_loop import _supersede_prior_round_repairs
    return _supersede_prior_round_repairs(plan_id, round_n, vp_ids)


def test_earlier_round_duplicate_for_the_same_vp_is_skipped(
    monkeypatch, tmp_path, db_path,
):
    monkeypatch.setattr(
        server, "_state_db_path", lambda *_a, **_kw: Path(db_path),
    )
    runtime = [
        {**_repair_row("repair-r1-03", "pending", "VP-007", 1)},
        {**_repair_row("repair-r2-03", "pending", "VP-007", 2)},
        {**_repair_row("repair-r2-04", "pending", "VP-009", 2)},
    ]
    _seed_plan(tmp_path, "p-w5", [{"id": "x"}], runtime, db_path)

    retired = _supersede("p-w5", 2, ["VP-007"])

    assert retired == ["repair-r1-03"]
    from state_machine.db.connection import open as open_db
    conn = open_db(db_path)
    try:
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT task_id, status, failure_reason FROM plan_tasks "
                "WHERE plan_id = ?", ("p-w5",),
            ).fetchall()
        }
    finally:
        conn.close()
    assert rows["repair-r1-03"] == ("skipped", "superseded_by_round_2")
    # Same-round siblings and other VPs are untouched.
    assert rows["repair-r2-03"][0] == "pending"
    assert rows["repair-r2-04"][0] == "pending"


def test_supersede_only_touches_the_same_failed_vp_id(
    monkeypatch, tmp_path, db_path,
):
    """Scope guard: same ``failed_vp_id``, strictly earlier round, pending.

    Nothing else may be retired — not a different VP, not a row from this
    round, not a row that already ran.
    """
    monkeypatch.setattr(
        server, "_state_db_path", lambda *_a, **_kw: Path(db_path),
    )
    runtime = [
        {**_repair_row("repair-r1-01", "pending", "VP-007", 1)},   # same VP, older → retire
        {**_repair_row("repair-r1-02", "pending", "VP-008", 1)},   # other VP     → keep
        {**_repair_row("repair-r3-01", "pending", "VP-007", 3)},   # this round   → keep
        {**_repair_row("repair-r1-03", "completed", "VP-007", 1)}, # ran already  → keep
        {**_repair_row("repair-r1-04", "failed", "VP-007", 1)},    # ran already  → keep
    ]
    _seed_plan(tmp_path, "p-w5b", [{"id": "x"}], runtime, db_path)

    retired = _supersede("p-w5b", 3, ["VP-007"])
    assert retired == ["repair-r1-01"]

    from state_machine.db.connection import open as open_db
    conn = open_db(db_path)
    try:
        rows = dict(
            conn.execute(
                "SELECT task_id, status FROM plan_tasks WHERE plan_id = ?",
                ("p-w5b",),
            ).fetchall()
        )
    finally:
        conn.close()
    assert rows["repair-r1-02"] == "pending"
    assert rows["repair-r3-01"] == "pending"
    assert rows["repair-r1-03"] == "completed"
    assert rows["repair-r1-04"] == "failed"


def test_supersede_is_a_no_op_when_no_vp_ids_are_given(
    monkeypatch, tmp_path, db_path,
):
    monkeypatch.setattr(
        server, "_state_db_path", lambda *_a, **_kw: Path(db_path),
    )
    runtime = [{**_repair_row("repair-r1-01", "pending", "VP-007", 1)}]
    _seed_plan(tmp_path, "p-w5c", [{"id": "x"}], runtime, db_path)
    assert _supersede("p-w5c", 2, []) == []
    assert _supersede("p-w5c", 2, None) == []


def test_supersede_survives_a_missing_plan(monkeypatch, tmp_path, db_path):
    monkeypatch.setattr(
        server, "_state_db_path", lambda *_a, **_kw: Path(db_path),
    )
    assert _supersede("no-such-plan", 2, ["VP-007"]) == []


# ---------------------------------------------------------------------------
# W4a — the post-repair callback must consume the stop reason
# ---------------------------------------------------------------------------


def test_repair_path_stop_reason_reads_the_repair_verdict(monkeypatch):
    import verification_loop as vl
    monkeypatch.setattr(
        server, "_execution_state",
        {"p1": {"_source": "repair_execution",
                "stop_reason": "repair_exited_with_unfinished_tasks:30"}},
    )
    assert vl._repair_path_stop_reason("p1") == (
        "repair_exited_with_unfinished_tasks:30"
    )


def test_repair_path_stop_reason_is_none_for_a_main_execution(monkeypatch):
    """A main execution run's stop reason must not stop the repair chain."""
    import verification_loop as vl
    monkeypatch.setattr(
        server, "_execution_state",
        {"p1": {"_source": None, "stop_reason": "something_else"}},
    )
    assert vl._repair_path_stop_reason("p1") is None


def test_repair_path_stop_reason_is_none_when_there_is_no_record(monkeypatch):
    import verification_loop as vl
    monkeypatch.setattr(server, "_execution_state", {})
    assert vl._repair_path_stop_reason("nope") is None


def test_repair_path_stop_reason_is_none_when_the_round_succeeded(monkeypatch):
    import verification_loop as vl
    monkeypatch.setattr(
        server, "_execution_state",
        {"p1": {"_source": "repair_execution", "stop_reason": None}},
    )
    assert vl._repair_path_stop_reason("p1") is None


def test_guard_counter_increments_per_plan_and_resets(monkeypatch):
    import verification_loop as vl
    monkeypatch.setattr(vl, "_repair_not_drained_guard", {})
    assert vl._bump_repair_not_drained_guard("p1") == 1
    assert vl._bump_repair_not_drained_guard("p1") == 2
    # A second plan has its own budget.
    assert vl._bump_repair_not_drained_guard("p2") == 1
    vl._clear_repair_not_drained_guard("p1")
    assert vl._bump_repair_not_drained_guard("p1") == 1
    # Clearing an unknown plan is harmless.
    vl._clear_repair_not_drained_guard("p2")
    vl._clear_repair_not_drained_guard("never-seen")


def test_guard_cap_is_independent_of_the_verification_round_budget():
    """The protection count must not be the round cap wearing a hat.

    ``DEFAULT_MAX_VERIFICATION_ROUNDS`` bounds how many verification
    rounds a plan may run. This bounds how many times the post-repair
    callback may refuse to re-verify because the batch never drained —
    a different failure with a different natural bound, so it gets its
    own constant.
    """
    import verification_loop as vl
    assert vl._MAX_REPAIR_NOT_DRAINED_GUARDS == 3
    assert vl._MAX_REPAIR_NOT_DRAINED_GUARDS != getattr(
        vl, "DEFAULT_MAX_VERIFICATION_ROUNDS", None,
    )
    assert vl._MAX_REPAIR_NOT_DRAINED_GUARDS != getattr(
        vl, "_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS", None,
    )


def test_both_signals_are_needed_before_stopping(monkeypatch, tmp_path, db_path):
    """Stop reason alone is not enough; so is a non-empty queue alone.

    The callback requires BOTH: the repair path recorded unfinished
    tasks, AND ``_get_pending_repair_tasks`` still finds queued work.
    This test pins that the queue really does report the rows left
    behind, since that is the half most likely to rot.
    """
    monkeypatch.setattr(server, "_state_db_path", lambda *_a, **_kw: Path(db_path))
    runtime = [
        _repair_row("repair-r1-01", "pending", "VP-005", 1),
        _repair_row("repair-r1-02", "pending", "VP-006", 1),
        _repair_row("repair-r1-03", "completed", "VP-007", 1),
    ]
    _seed_plan(tmp_path, "p-w4a", [{"id": "x"}], runtime, db_path)

    import verification_loop as vl
    pending = vl._get_pending_repair_tasks("p-w4a")
    assert sorted(t["title"] for t in pending) == ["", ""]
    assert len(pending) == 2, "the completed row must not stay in the queue"
