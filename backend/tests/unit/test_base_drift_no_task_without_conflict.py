"""Only a **conflict** becomes a task — every other outcome becomes none.

The base-drift check has seven outcomes and exactly one of them is a
statement about the branch: ``conflicts``.  The other six — the branch
is in sync, the base merged cleanly, the working tree was dirty, a git
call failed, the check was skipped, the check is switched off — are the
check's own housekeeping.  None of them is a defect in the product, and
a round that stopped on any of them would be blocking for reasons that
have nothing to do with the code under test.

A conflict now becomes a **task** (see
``test_base_drift_report_mode_persist.py``), so the same question has to
be asked of the write side: which outcomes put a row in ``state.db``?
The answer has to be exactly one — ``conflicts`` — and this file is the
negative control that pins it.  A gate that lets a ``dirty_tree`` or a
``skipped`` check become a repair task sends the executor off to fix a
branch that was never broken.

The reports below carry conflicting files **on purpose**.  A report that
names no conflicting file cannot become a task whatever the gate does:
the builder refuses to build one.  Such a report would prove nothing
about the gate, and a negative control that cannot fail is not a
control.  Carrying the files leaves the *status* as the only thing
standing between the report and a task row — which is exactly the
property under test.

The properties pinned here:

* none of the six non-conflicting outcomes writes a base-drift task;
* a conflict that names no conflicting file blocks the round and still
  writes no task — there is nothing to repair, and a task without a
  change scope sends the executor off to "fix" files that were never in
  conflict;
* a conflict that names a conflicting file writes **exactly one** task
  — the positive control, without which the negative controls above
  could be passing because the machinery never writes anything at all;
* a non-conflicting outcome lets the round go on.

Nothing here stubs the persistence: the rows are written by the real
``persist_conflict_repair_task`` into the real ``state.db`` — the
scratch copy of it that the autouse fixture points ``PDT_STATE_DB_PATH``
at — and read back through the repository the executor reads.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import base_drift  # noqa: E402
import verification_agent  # noqa: E402
from base_drift import (  # noqa: E402
    BASE_DRIFT_VP_ID,
    STATUS_CONFLICTS,
    STATUS_DIRTY_TREE,
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_MERGED,
    STATUS_NONE,
    STATUS_SKIPPED,
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

NON_CONFLICTING_STATUSES = (
    STATUS_NONE,
    STATUS_MERGED,
    STATUS_DIRTY_TREE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_DISABLED,
)
"""The six outcomes that are the check's housekeeping, not a statement
about the branch.  The set is closed: ``conflicts`` is the seventh, and
it is the only one that may block or write."""


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


def _report(status: str, **over) -> BaseDriftReport:
    """A base-drift report in the given status.

    The report carries a conflicting file unless the caller says
    otherwise: that is what keeps the status the only thing standing
    between the report and a task row.
    """
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


def _stub_check(monkeypatch, report) -> None:
    """Make the check report what the test wants it to report.

    Only the *check* is stubbed.  The persistence is the real thing,
    writing into the real database.
    """
    monkeypatch.setattr(
        base_drift, "check_and_merge_base", lambda *a, **k: report
    )


def _rows(plan_id: str = PLAN_ID) -> Dict[str, Dict[str, Any]]:
    """Every task row of ``plan_id``, read back through the repository.

    Read through the repository, so what is asserted is what the
    executor would see — not what the writer believed it wrote.
    """
    conn = open_db(resolve_state_db_path())
    try:
        try:
            return PlanTaskRepository(conn).load_all(plan_id)
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            # Nothing was ever written to this database, so the table
            # was never created.  That is the negative control's point.
            return {}
    finally:
        conn.close()


def _base_drift_rows(plan_id: str = PLAN_ID) -> List[Dict[str, Any]]:
    """The base-drift task rows of ``plan_id``."""
    return [
        row
        for row in _rows(plan_id).values()
        if row.get("failed_vp_id") == BASE_DRIFT_VP_ID
    ]


# ---------------------------------------------------------------------------
# The negative controls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", NON_CONFLICTING_STATUSES)
def test_non_conflicting_status_writes_no_task(monkeypatch, tmp_path, status):
    """No outcome but a conflict puts a row in the queue.

    The report carries a conflicting file, so the *only* thing that can
    stop this report from becoming a task is the status gate.  A gate
    that lets ``dirty_tree`` or ``skipped`` through would queue a
    repair for a branch nobody broke — and the executor would go and
    "fix" it.
    """
    orch = _orchestrator(tmp_path)
    _stub_check(monkeypatch, _report(status))

    orch._run_base_drift_preflight()

    assert _base_drift_rows() == [], status


def test_conflict_without_files_writes_no_task(monkeypatch, tmp_path):
    """A conflict that names no file blocks the round and writes no task.

    The conflict is real — the round must not verify a tree that is not
    the tree anybody meant to verify — so the round stays BLOCKED.  But
    there is nothing to repair: a task without a change scope sends the
    executor off to "fix" files that were never in conflict.
    """
    orch = _orchestrator(tmp_path)
    _stub_check(monkeypatch, _report(STATUS_CONFLICTS, conflict_files=[]))

    blocked = orch._run_base_drift_preflight()

    assert blocked is not None
    assert blocked["overall_status"] == "BLOCKED"
    assert blocked["stop_reason"] == "base_drift_conflicts"
    assert _base_drift_rows() == []


@pytest.mark.parametrize("status", NON_CONFLICTING_STATUSES)
def test_non_conflicting_status_keeps_the_round_running(
    monkeypatch, tmp_path, status
):
    """None of the six lets the preflight stop the round.

    A check that blocks on its own problems blocks for reasons that
    have nothing to do with the code under test: the round would go
    BLOCKED on a dirty working tree and nobody would be able to say
    why the product was never looked at.
    """
    orch = _orchestrator(tmp_path)
    _stub_check(monkeypatch, _report(status))

    blocked = orch._run_base_drift_preflight()

    assert blocked is None, status


# ---------------------------------------------------------------------------
# The positive control
# ---------------------------------------------------------------------------


def test_conflict_with_files_writes_exactly_one_task(monkeypatch, tmp_path):
    """A conflict that names a file writes exactly one task.

    Without this, the negative controls above would be passing because
    the machinery never writes anything — including when it should.
    One row, not two: a queue that holds the same conflict twice holds
    it twice.
    """
    orch = _orchestrator(tmp_path)
    _stub_check(monkeypatch, _report(STATUS_CONFLICTS))

    blocked = orch._run_base_drift_preflight()

    assert blocked is not None
    assert blocked["base_drift_repair_task_id"] == TASK_ID

    rows = _base_drift_rows()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["status"] == "pending", row
    assert row["task_group"] == "repair", row
    assert row["files_to_modify"] == ["app.py"], row