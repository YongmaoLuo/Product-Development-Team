"""The base-drift preflight that runs before every verification round.

A verification round answers "does the code on this branch still work?".
When the branch has drifted away from its base (``main``), the round
measures its own staleness instead of the work under test: a test that
used to pass fails because *the base* changed the contract it depends on,
and the round reports "the feature is broken" about a feature nobody
touched.

This preflight is the one place that knows about the drift.  It runs
**once per round**, before the plan is generated, and it either lets the
round go on or stops it with a BLOCKED report that says why.

The properties pinned here:

* the check runs **once per round, before the plan** — a check that ran
  inside the verification-point loop would re-merge the base under the
  round's own feet;
* **every** outcome writes an event, not only the blocking one: the log
  has to be able to say "the branch was in sync" too;
* only ``conflicts`` blocks.  A dirty tree, a failed git call, a skipped
  check and a disabled check all leave the round running — a check that
  blocks on its own problems blocks for reasons that have nothing to do
  with the code under test;
* a conflict becomes a task through the repair channel that **already
  exists** — in every mode, ``report`` included.  The mode says what the
  *check* may do to the working tree, not what the round may write
  down: a conflict that is only reported is a conflict nobody ever
  resolves.  This file does not write to the database: it builds the
  task and hands it to
  :func:`verification_agent.persist_conflict_repair_task`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import base_drift  # noqa: E402
import verification_agent  # noqa: E402
from base_drift import (  # noqa: E402
    STATUS_CONFLICTS,
    STATUS_DIRTY_TREE,
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_MERGED,
    STATUS_NONE,
    STATUS_SKIPPED,
    BaseDriftReport,
)
from tests.app_source import find_def  # noqa: E402


PLAN_DIR_NAME = "plan-20261029"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakePersistence:
    """The two persistence surfaces the preflight touches."""

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
    orch._current_round = 3
    orch._last_base_drift_report = None
    return orch


def _report(status: str, **over) -> BaseDriftReport:
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


def _stub_the_round(orch):
    """Make ``run_full_verification`` reach the preflight and no further.

    The round's own bookkeeping (clearing the verdict cache, opening the
    round, resetting the per-round caches) is not what this file tests —
    it is stubbed so the test can look at the one thing that is.
    """
    orch._clear_verification_state_files = lambda: None

    def fake_start(round_number):
        orch._current_round = round_number

    orch.start_verification_round = fake_start
    orch._freshness_cache = SimpleNamespace(clear=lambda: None)
    orch._rebuild_coordinator = SimpleNamespace(clear=lambda: None)


def _dotted(node) -> str:
    """``self.foo.bar`` for the call target of a call expression."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


# ---------------------------------------------------------------------------
# The round that goes on
# ---------------------------------------------------------------------------


def test_no_drift_returns_none_and_logs_event(monkeypatch, tmp_path):
    """A branch that is in sync lets the round go on — and says so.

    The event is written for the *non-blocking* outcomes too.  A log that
    only records the problems is a log that cannot answer "did the check
    run?", and "the check silently did not run" and "the branch was in
    sync" have to be told apart.
    """
    orch = _orchestrator(tmp_path)
    monkeypatch.setattr(
        base_drift,
        "check_and_merge_base",
        lambda *a, **k: _report(STATUS_NONE),
    )

    blocked = orch._run_base_drift_preflight()

    assert blocked is None, blocked
    assert len(orch.persistence.events) == 1, orch.persistence.events
    vp_id, event_type, data = orch.persistence.events[0]
    assert vp_id == "__preflight__"
    assert event_type == "base_drift_none"
    assert data["status"] == "none"
    assert data["base_ref"] == "origin/main"


@pytest.mark.parametrize(
    "status, event",
    [
        (STATUS_MERGED, "base_drift_merged"),
        (STATUS_DIRTY_TREE, "base_drift_dirty_tree"),
        (STATUS_FAILED, "base_drift_failed"),
        (STATUS_SKIPPED, "base_drift_skipped"),
        (STATUS_DISABLED, "base_drift_disabled"),
    ],
)
def test_non_conflicting_statuses_do_not_block(
    monkeypatch, tmp_path, status, event,
):
    """Only ``conflicts`` stops the round.

    The other statuses are the check's own housekeeping: the base was
    merged in, the tree was dirty, a git call failed, the directory is
    not a work tree, the check is switched off.  None of them is a
    statement about the product, and none of them may cost the round its
    verification points.
    """
    orch = _orchestrator(tmp_path)
    monkeypatch.setattr(
        base_drift,
        "check_and_merge_base",
        lambda *a, **k: _report(status),
    )

    blocked = orch._run_base_drift_preflight()

    assert blocked is None, blocked
    assert orch.persistence.events[0][1] == event


def test_disabled_status_does_not_block(monkeypatch, tmp_path):
    """A check that is switched off blocks nothing.

    ``PDT_BASE_DRIFT_ENABLED=0`` turns the check off.  Turning a check
    off must not turn the round off with it.
    """
    orch = _orchestrator(tmp_path)
    monkeypatch.setattr(
        base_drift,
        "check_and_merge_base",
        lambda *a, **k: _report(STATUS_DISABLED),
    )

    blocked = orch._run_base_drift_preflight()

    assert blocked is None, blocked
    assert orch.persistence.events[0][1] == "base_drift_disabled"


# ---------------------------------------------------------------------------
# The round that stops
# ---------------------------------------------------------------------------


def test_conflict_returns_blocked_report(monkeypatch, tmp_path):
    """A conflict stops the round, and the report says what conflicted.

    The round dispatched nothing, so it measured nothing about the
    product: the report is ``BLOCKED`` and carries **no** verification
    results.  A FAILED verdict would send the repair chain after a
    product nobody ever looked at.
    """
    orch = _orchestrator(tmp_path)
    monkeypatch.setattr(
        base_drift,
        "check_and_merge_base",
        lambda *a, **k: _report(STATUS_CONFLICTS),
    )

    blocked = orch._run_base_drift_preflight()

    assert blocked is not None
    assert blocked["overall_status"] == "BLOCKED"
    assert blocked["verification_results"] == []
    assert blocked["stop_reason"] == "base_drift_conflicts"
    assert blocked["execution_profile"] == {"mode": "base_drift_blocked"}

    conflicts = blocked["base_drift_conflicts"]
    assert conflicts["status"] == "conflicts"
    assert conflicts["base_ref"] == "origin/main"
    assert conflicts["base_sha"] == "abc1234"
    assert conflicts["conflict_files"] == [
        {"path": "app.py", "conflict_blocks": 1}
    ]

    # The event says the same thing the report says.
    assert orch.persistence.events[0][1] == "base_drift_conflicts"


def test_repair_mode_calls_persist_and_stamps_id(monkeypatch, tmp_path):
    """In ``repair`` mode the conflict becomes a task, through the
    repair channel that already exists.

    The preflight does not write to the database itself: it builds the
    task and hands it to
    :func:`verification_agent.persist_conflict_repair_task`, which owns
    the write.  The report then names the task, so a reader can tell
    *which* repair the blocked round is waiting for.
    """
    orch = _orchestrator(tmp_path)
    monkeypatch.setattr(
        base_drift,
        "check_and_merge_base",
        lambda *a, **k: _report(STATUS_CONFLICTS, mode="repair"),
    )
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
    assert plan_id == PLAN_DIR_NAME
    assert task["id"] == "repair-r3-01"
    assert task["files_to_modify"] == ["app.py"]
    assert task["task_group"] == "repair"
    assert blocked["base_drift_repair_task_id"] == "repair-r3-01"


def test_repair_mode_without_task_falls_back_to_report(monkeypatch, tmp_path):
    """No task to build → still BLOCKED, and no task id.

    The builder returns ``None`` when the report names no conflicting
    file: there is nothing to repair, and a task without a change scope
    sends the executor off to "fix" files that were never in conflict.
    The round stays blocked — the conflict is real — but it does not
    claim a repair task that does not exist.
    """
    orch = _orchestrator(tmp_path)
    monkeypatch.setattr(
        base_drift,
        "check_and_merge_base",
        lambda *a, **k: _report(STATUS_CONFLICTS, mode="repair"),
    )
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


# ---------------------------------------------------------------------------
# The call site
# ---------------------------------------------------------------------------


def test_call_site_persists_blocked_report(monkeypatch, tmp_path):
    """A blocked round is written down, and the round stops there.

    The report has to reach the persistence layer — a BLOCKED round that
    nobody can read is a round that looks like it never happened.  And
    the round must not carry on: no verification plan, no service
    preflight, no verification point dispatched against a tree that is
    in conflict with its base.
    """
    orch = _orchestrator(tmp_path)
    _stub_the_round(orch)
    monkeypatch.setattr(
        base_drift,
        "check_and_merge_base",
        lambda *a, **k: _report(STATUS_CONFLICTS),
    )
    reached = []
    monkeypatch.setattr(
        verification_agent.Orchestrator,
        "generate_verification_plan",
        lambda self, *a, **k: reached.append("plan"),
    )
    monkeypatch.setattr(
        verification_agent.Orchestrator,
        "_run_round_service_preflight",
        lambda self, *a, **k: reached.append("service_preflight"),
    )

    report = orch.run_full_verification(round_number=3)

    assert report["overall_status"] == "BLOCKED"
    assert report["stop_reason"] == "base_drift_conflicts"
    assert len(orch.persistence.reports) == 1, orch.persistence.reports
    assert orch.persistence.reports[0]["stop_reason"] == "base_drift_conflicts"
    assert reached == [], reached


def test_call_site_is_once_per_round_before_plan():
    """The preflight sits once per round, between the round's reset and
    the plan.

    *Once*: a check that ran per verification point would re-merge the
    base under the round's own feet, and the round's report would depend
    on how many verification points it happened to have.

    *Before the plan*: the plan is what the round executes; a round that
    discovers the conflict after the plan exists has already spent the
    LLM call that produced it, and the conflict is a fact about the
    branch — it is knowable before anything is planned.
    """
    found = find_def("run_full_verification")
    calls = [
        node for node in ast.walk(found.node) if isinstance(node, ast.Call)
    ]
    preflight = [
        c for c in calls
        if _dotted(c.func) == "self._run_base_drift_preflight"
    ]
    plan = [
        c for c in calls
        if _dotted(c.func) == "self.generate_verification_plan"
    ]
    reset = [
        c for c in calls
        if _dotted(c.func) == "self._rebuild_coordinator.clear"
    ]

    assert len(preflight) == 1, [
        _dotted(c.func) for c in calls
    ]
    assert plan, "the round no longer generates a verification plan"
    assert reset, "the round no longer resets the rebuild coordinator"
    assert reset[0].lineno < preflight[0].lineno < plan[0].lineno, (
        f"the preflight must run after the round's reset "
        f"({reset[0].lineno}) and before the plan ({plan[0].lineno}), "
        f"it sits at {preflight[0].lineno}"
    )
