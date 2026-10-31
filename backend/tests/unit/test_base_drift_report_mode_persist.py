"""A base-drift conflict becomes a task — in **every** mode.

The preflight used to write a repair task only in ``repair`` mode, and
the preflight runs in ``report`` mode by default.  So a conflict became
a *report* and nothing else: no task, no row, nothing the executor
could pick up.  The plan then sat on a BLOCKED round until it went
``failed / loop_stopped`` — the conflict was described, never handed to
anyone who could resolve it.

The mode says what the **check** may do to the working tree (only
``repair`` merges or reverts anything); it does not say what the round
may write down.  A conflict is a fact about the branch, and a fact
nobody can act on is not a fact the round has produced.

The properties pinned here:

* a conflicting report produces a task in **every** mode — ``report``
  included;
* the task lands in ``state.db`` as an **executable** row: it carries a
  title, a test command that says "the merge is done", the repair group
  and the base-drift verification point;
* the BLOCKED report keeps naming the conflicting files **and** names
  the task it is now waiting for;
* a report that names no conflicting file produces **no** task — a task
  without a change scope sends the executor off to "fix" files that
  were never in conflict;
* ``repair`` mode keeps working exactly as it did.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import base_drift  # noqa: E402
import verification_agent  # noqa: E402
from base_drift import (  # noqa: E402
    BASE_DRIFT_VP_ID,
    STATUS_CONFLICTS,
    BaseDriftReport,
)
from config_paths import resolve_state_db_path  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)


PLAN_DIR_NAME = "plan-20261031"
PLAN_ID = PLAN_DIR_NAME
ROUND = 3
TASK_ID = f"repair-r{ROUND}-01"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakePersistence:
    """The persistence surface the preflight touches."""

    def __init__(self) -> None:
        self.events = []
        self.reports = []

    def write_verification_point_log(self, vp_id, event_type, data):
        self.events.append((vp_id, event_type, data))

    def update_report(self, report_data):
        self.reports.append(report_data)


def _orchestrator(tmp_path: Path):
    """An orchestrator carrying only the attributes the preflight reads.

    Built with ``__new__`` on purpose: the preflight must not depend on
    a fully constructed orchestrator, and every attribute it touches is
    read defensively.
    """
    orch = verification_agent.Orchestrator.__new__(
        verification_agent.Orchestrator
    )
    orch.project_dir = tmp_path
    orch.plan_dir = tmp_path / PLAN_DIR_NAME
    orch.persistence = _FakePersistence()
    orch._current_round = ROUND
    orch._last_base_drift_report = None
    return orch


def _report(status: str = STATUS_CONFLICTS, **over) -> BaseDriftReport:
    """A base-drift report in the given status."""
    payload = {
        "status": status,
        "base_ref": "origin/main",
        "base_sha": "abc1234",
        "behind": 2,
        "ahead": 0,
        "conflict_files": [{"path": "app.py", "conflict_blocks": 1}],
        "mode": "report",
    }
    payload.update(over)
    return BaseDriftReport(**payload)


def _stub_conflict(monkeypatch, report=None) -> None:
    """Make the check report the given conflict (a conflicting one)."""
    monkeypatch.setattr(
        base_drift,
        "check_and_merge_base",
        lambda *a, **k: report if report is not None else _report(),
    )


def _rows(db_path, plan_id: str = PLAN_ID):
    """Every task row of ``plan_id``, read back through the repository."""
    conn = open_db(db_path)
    try:
        return PlanTaskRepository(conn).load_all(plan_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The conflict becomes a task
# ---------------------------------------------------------------------------


def test_report_mode_conflict_yields_a_task_id(monkeypatch, tmp_path):
    """A conflict in the **default** mode becomes a task the report names.

    The default mode is ``report``, and the preflight is what runs by
    default.  A conflict there used to produce a report and nothing
    else — the round blocked and no task existed for anyone to pick up.
    The mode decides what the *check* may do to the working tree; the
    round still has to hand the conflict to the executor.
    """
    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch, _report(STATUS_CONFLICTS, mode="report"))
    calls = []

    def fake_persist(plan_id, task):
        calls.append((plan_id, task))
        return True

    monkeypatch.setattr(
        verification_agent, "persist_conflict_repair_task", fake_persist
    )

    blocked = orch._run_base_drift_preflight()

    assert len(calls) == 1, calls
    plan_id, task = calls[0]
    assert plan_id == PLAN_ID
    assert task["id"] == TASK_ID
    assert task["task_group"] == "repair"
    assert task["failed_vp_id"] == BASE_DRIFT_VP_ID
    assert task["files_to_modify"] == ["app.py"]
    assert blocked["base_drift_repair_task_id"] == TASK_ID


def test_report_mode_task_lands_in_state_db(monkeypatch, tmp_path):
    """The task is really written — and it is an executable row.

    A task that only exists in a local variable resolves nothing.  The
    row has to be in the database the executor reads, and it has to
    carry what the executor needs to run it: a title (the loader merges
    a row's content only when a title *and* a description are there), a
    test command that says "the merge is really done", the repair group
    and the verification point the repair is filed under.
    """
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    assert resolve_state_db_path() == db_path

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch, _report(STATUS_CONFLICTS, mode="report"))

    blocked = orch._run_base_drift_preflight()

    assert blocked["base_drift_repair_task_id"] == TASK_ID

    rows = _rows(resolve_state_db_path())
    assert list(rows) == [TASK_ID], rows
    row = rows[TASK_ID]
    assert row["status"] == "pending", row
    assert row["title"] == "合并 origin/main：解决 1 个文件的冲突", row
    assert row["test_command"] == (
        'git merge-base --is-ancestor abc1234 HEAD '
        '&& test -z "$(git ls-files -u)"'
    ), row
    assert row["task_group"] == "repair", row
    assert row["failed_vp_id"] == BASE_DRIFT_VP_ID, row


def test_blocked_report_still_lists_conflict_files(monkeypatch, tmp_path):
    """The task does not replace the evidence: the files stay named.

    The task says *what to fix*; the report says *what was wrong*.  A
    reader that only sees the task id cannot tell which files were in
    conflict, and a reader that only sees the files cannot tell whether
    anybody was asked to resolve them.  The blocked report carries both.
    """
    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch, _report(STATUS_CONFLICTS, mode="report"))
    monkeypatch.setattr(
        verification_agent, "persist_conflict_repair_task",
        lambda *a, **k: True,
    )

    blocked = orch._run_base_drift_preflight()

    assert blocked["overall_status"] == "BLOCKED"
    assert blocked["stop_reason"] == "base_drift_conflicts"
    conflicts = blocked["base_drift_conflicts"]
    assert conflicts["conflict_files"] == [
        {"path": "app.py", "conflict_blocks": 1}
    ], conflicts
    assert conflicts["base_drift_repair_task_id"] == TASK_ID


def test_no_task_when_builder_returns_none(monkeypatch, tmp_path):
    """No task to build → still BLOCKED, and no task id claimed.

    The builder returns ``None`` when the report names no conflicting
    file: there is nothing to repair, and a task without a change scope
    sends the executor off to "fix" files that were never in conflict.
    The round stays blocked — the conflict is real — but it does not
    claim a repair task that does not exist.
    """
    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch, _report(STATUS_CONFLICTS, mode="report"))
    monkeypatch.setattr(
        base_drift, "build_conflict_repair_task", lambda *a, **k: None
    )
    calls = []
    monkeypatch.setattr(
        verification_agent,
        "persist_conflict_repair_task",
        lambda *a, **k: calls.append(a) or True,
    )

    blocked = orch._run_base_drift_preflight()

    assert calls == [], calls
    assert blocked is not None
    assert "base_drift_repair_task_id" not in blocked
    assert blocked["overall_status"] == "BLOCKED"
    assert blocked["stop_reason"] == "base_drift_conflicts"


def test_report_without_conflict_files_yields_no_task(monkeypatch, tmp_path):
    """A conflict report that names no file produces no task either.

    The builder is not stubbed here: this is the shape a real report
    with an empty conflict list takes, and the round must not invent a
    repair for a conflict it cannot name.
    """
    orch = _orchestrator(tmp_path)
    _stub_conflict(
        monkeypatch,
        _report(STATUS_CONFLICTS, mode="report", conflict_files=[]),
    )
    calls = []
    monkeypatch.setattr(
        verification_agent,
        "persist_conflict_repair_task",
        lambda *a, **k: calls.append(a) or True,
    )

    blocked = orch._run_base_drift_preflight()

    assert calls == [], calls
    assert "base_drift_repair_task_id" not in blocked
    assert blocked["overall_status"] == "BLOCKED"


def test_storage_failure_only_warns(monkeypatch, tmp_path, caplog):
    """A row that cannot be written is reported, not raised.

    The conflict is real whatever the database is doing, so the round
    stays blocked and says why.  What must not happen is the write
    failure taking the round down with it: the preflight never raises,
    and the loss is written down as a warning.
    """
    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch, _report(STATUS_CONFLICTS, mode="report"))

    def boom(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(
        verification_agent, "persist_conflict_repair_task", boom
    )

    with caplog.at_level("WARNING", logger="verification_agent"):
        blocked = orch._run_base_drift_preflight()

    assert blocked is not None
    assert blocked["overall_status"] == "BLOCKED"
    assert blocked["stop_reason"] == "base_drift_conflicts"
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "verification_agent"
    ]
    assert any("could not be" in message for message in warnings), warnings


def test_repair_mode_still_persists(monkeypatch, tmp_path):
    """``repair`` mode is unchanged: same task, same id, same channel.

    The gate that used to keep ``report`` mode from writing is gone;
    ``repair`` mode must not be the one that changed behaviour.
    """
    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch, _report(STATUS_CONFLICTS, mode="repair"))
    calls = []

    def fake_persist(plan_id, task):
        calls.append((plan_id, task))
        return True

    monkeypatch.setattr(
        verification_agent, "persist_conflict_repair_task", fake_persist
    )

    blocked = orch._run_base_drift_preflight()

    assert len(calls) == 1, calls
    plan_id, task = calls[0]
    assert plan_id == PLAN_ID
    assert task["id"] == TASK_ID
    assert task["files_to_modify"] == ["app.py"]
    assert blocked["base_drift_repair_task_id"] == TASK_ID
