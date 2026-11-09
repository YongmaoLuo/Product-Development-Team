"""watcher 结束时，把「这一轮为什么结束」写进数据库那一行。

背景
----
executor 非零退出时，watcher 是知道原因的：它把
``state["stop_reason"] = "non_zero_exit:1"`` 放进内存里的状态。但那个
理由一直只活在内存里 —— 最终落库的那一步调的是
``update_status(plan_id, status)``，没有带理由，于是
``plan_execution.stop_reason`` 那一列一直挂着上一次开机 reconcile
写的 ``startup_reconcile``。读这一列的人读到的是一个已经过期的解释。

这里钉的是**接线**，不是仓储的方法：走真正的生产入口
（``start_execution`` → watcher → 数据库），断言那一列里最后是
``non_zero_exit:1``。只测 ``ExecutionRepository.update_status`` 的话，
watcher 忘了传理由，测试照样是绿的 —— 而那正是这个 bug 的形状。

另外钉住的是「不传就不覆盖」：watcher 没有理由可报时（正常完成，
``stop_reason`` 是 ``None``），那一列必须保持原样，不能被写成空串，
也不能把上一次的理由抹掉。
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


_PLAN_ID = "watcher-stop-reason-plan"


# ---------------------------------------------------------------------------
# helpers
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


def _seed_plan(db_path: Path, plan_id: str, project_dir: Path) -> None:
    """Insert the ``plan_routing`` / ``plan_execution`` rows a start needs.

    The execution row starts with ``stop_reason`` already set — the
    startup reconcile's.  That is the whole point: a stale reason is
    what the row carries until something overwrites it.
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
                plan_id, None, "queued", 0, "[]",
                "{}", "{}",
                json.dumps(
                    {
                        "status": "pending",
                        "round": 0,
                        "max_rounds": 3,
                        "stop_reason": None,
                    }
                ),
                "2026-11-07T00:00:00", "2026-11-07T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution "
            "(plan_id, attempt_count, project_dir, stop_reason, task_progress, "
            " next_run_at, card_state, flags, exec_pid, exec_status, "
            " started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, 0, str(project_dir), "startup_reconcile",
                json.dumps({"tasks": {}}),
                None, "{}", "{}", None, "not_started", None,
                "2026-11-07T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _read_execution_row(db_path: Path, plan_id: str) -> dict:
    """Read the plan's ``plan_execution`` row."""
    from state_machine.db.connection import open as open_db

    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT exec_status, stop_reason, exec_pid FROM plan_execution "
            "WHERE plan_id = ?",
            (plan_id,),
        )
        row = cur.fetchone()
        assert row is not None, "the plan has no execution row"
        return {
            "exec_status": row[0],
            "stop_reason": row[1],
            "exec_pid": row[2],
        }
    finally:
        conn.close()


class _FakeProcess:
    """Minimal stand-in for the executor ``subprocess.Popen`` handle."""

    def __init__(self, returncode: int) -> None:
        self.pid = 424242
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode


def _drive_watcher(
    tmp_path, monkeypatch, *, returncode: int, pending: int = 0
) -> Path:
    """Run the real watcher body to completion; return the state db path.

    The watcher is a daemon thread in production; the test hands the body
    over instead (``_run_in_plan_ctx``), so the assertions cannot race
    the thread.

    ``pending`` is how many tasks the watcher still finds unfinished —
    ``0`` is the clean run, anything above that is the "exited with
    unfinished tasks" disposition.
    """
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / _PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "1", "title": "t", "depends_on": []}]}),
        encoding="utf-8",
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    db_path = _make_state_db(tmp_path)
    _seed_plan(db_path, _PLAN_ID, project_dir)

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
        "_count_unfinished_tasks",
        lambda *a, **k: {
            "total": pending,
            "pending_or_unstarted": pending,
            "failed": 0,
            "unfinished_ids": [str(i) for i in range(pending)],
        },
    )
    # The verification round is not what this test is about, and a real
    # one would call out to a provider.
    monkeypatch.setattr(
        server, "_run_auto_verification_loop", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        server, "_settle_phase_after_verification_loop", lambda *a, **k: None,
    )

    transitions: list[str] = []

    class _FakePlanState:
        """Stands in for ``server.PlanState`` — the name the route uses."""

        def __init__(self, plan_dir_arg):
            self.plan_dir = plan_dir_arg

        def transition_to(self, phase):
            transitions.append(phase)

        def force_set_phase(self, phase):
            transitions.append(phase)

    monkeypatch.setattr(server, "PlanState", _FakePlanState)

    captured: list = []
    monkeypatch.setattr(
        server, "_run_in_plan_ctx", lambda plan_id_arg, fn: captured.append(fn),
    )

    server._execution_state.pop(_PLAN_ID, None)
    try:
        server.start_execution(
            _PLAN_ID,
            server.StartExecutionRequest(project_dir=str(project_dir)),
        )
        deadline = time.monotonic() + 10.0
        while not captured and time.monotonic() < deadline:
            time.sleep(0.01)
        assert captured, "the execution watcher never handed over"
        captured[0]()
    finally:
        server._execution_state.pop(_PLAN_ID, None)

    return db_path


# ---------------------------------------------------------------------------
# the watcher persists the reason
# ---------------------------------------------------------------------------


def test_non_zero_exit_reason_reaches_the_database_row(tmp_path, monkeypatch):
    """executor 非零退出 → 数据库那一列里是 ``non_zero_exit:1``。

    落库之前那一列挂着开机 reconcile 写的 ``startup_reconcile`` ——
    一个和这一轮毫无关系的解释。理由里带着退出码：``non_zero_exit``
    只说了「非零退出」，退出 1 和退出 7 在这一列里长得一模一样。
    """
    db_path = _drive_watcher(tmp_path, monkeypatch, returncode=1)

    row = _read_execution_row(db_path, _PLAN_ID)
    assert row["exec_status"] == "failed"
    assert row["stop_reason"] == "non_zero_exit:1", (
        "the row still carries the reason of whatever wrote it last"
    )


def test_clean_exit_leaves_an_existing_reason_alone(tmp_path, monkeypatch):
    """没有理由可报时，不传 —— 那一列保持原样，不被抹掉。

    正常完成时 ``state["stop_reason"]`` 是 ``None``；传 ``None`` 等于
    「我没有什么可说的」，不是「把理由清空」。
    """
    db_path = _drive_watcher(tmp_path, monkeypatch, returncode=0, pending=0)

    row = _read_execution_row(db_path, _PLAN_ID)
    assert row["exec_status"] == "completed"
    assert row["stop_reason"] == "startup_reconcile", (
        "a watcher with no reason to report must not overwrite the row's "
        "existing reason"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
