"""The auto-verification hand-off must key off durable facts, not a mutable status.

Background (2026-11-03)
----------------------
``routes/execution.py``'s executor watcher gated the *entire* completion
branch on ``if state["status"] == "running":`` — an in-memory field that
any other writer may change while the executor subprocess is still alive
(``stop_execution`` writes ``"stopped"``; the pause path writes
``"pausing"``).  On 2026-11-03 the status drifted to ``"stopped"``
*while the executor was still running*; the subprocess then exited 0, and
the whole completion branch — including the auto-verification hand-off —
was skipped.  No log line, no verification round, the plan simply stopped.

The fix makes the hand-off key off facts that survive a status drift:

    rc == 0  AND  pending_or_unstarted == 0  AND  no quota marker

Those three are read from disk (``tasks.json``) and from the runtime
overlay, not from a field another thread can flip mid-flight.

Contract pinned here
--------------------
* status drifted (``stopped``) + rc == 0 + nothing pending
  → the hand-off fires exactly once and the run reads ``completed``.
* status drifted + rc != 0 → no hand-off (unchanged behaviour).
* status drifted + rc == 0 + work still pending → no hand-off.
* status drifted + rc == 0 + a ``waiting_for_quota`` marker on
  ``tasks.json`` → no hand-off (the plan is parked, not finished).
* status drifted + rc == 0 + an unreadable ``tasks.json`` → hand-off
  still fires: ``_count_unfinished_tasks`` reports all zeros for a task
  file it cannot read ("nothing detected as unfinished"), and the gate
  inherits that documented contract instead of stranding the plan.

Negative control (run it once by hand, not by pytest)
-----------------------------------------------------
This file must be able to fail without the fix — otherwise it proves
nothing.  Both windows below were run by hand for this change; the
"uncommitted fix" window is the one the task brief prescribes.

**While the fix is still uncommitted** (from the repo root):

    git diff backend/routes/execution.py > /tmp/handoff.patch \
        && git checkout -- backend/routes/execution.py
    cd backend && <venv>/python3 -m pytest \
        tests/unit/test_auto_verification_handoff_durable.py -v
    # → test_handoff_fires_when_status_drifted_and_exit_is_zero MUST FAIL
    cd .. && git apply /tmp/handoff.patch
    cd backend && <venv>/python3 -m pytest \
        tests/unit/test_auto_verification_handoff_durable.py -v
    # → green again

``git checkout --`` reverts the *whole file* to its committed state, so
it discards every other uncommitted change in that file too — use it
only when the fix is that file's only uncommitted change.  The
equivalent that touches nothing but the recorded diff is:

    git diff backend/routes/execution.py > /tmp/handoff.patch \
        && git apply -R /tmp/handoff.patch

**Once the fix is committed** — ``git checkout --`` would then restore
the *fixed* file (HEAD carries the fix), so the "revert" would be a
no-op that still reports green.  Capture the diff from the commit that
introduced the fix instead:

    git show <fix-commit> -- backend/routes/execution.py \
        > /tmp/handoff.patch
    git apply -R /tmp/handoff.patch
    cd backend && <venv>/python3 -m pytest \
        tests/unit/test_auto_verification_handoff_durable.py -v
    # → test_handoff_fires_when_status_drifted_and_exit_is_zero MUST FAIL
    cd .. && git apply /tmp/handoff.patch
    cd backend && <venv>/python3 -m pytest \
        tests/unit/test_auto_verification_handoff_durable.py -v
    # → green again

``git apply -R`` / ``git apply`` touches only the recorded diff, so it
never throws away another writer's uncommitted change in that file —
prefer it over ``git checkout --`` whenever the file is not clean.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from tests.unit.test_executor_watcher_handoff import (
    _FakeProcess,
    _make_state_db,
    _release_and_join,
    _seed_plan,
)

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Fixture — a plan whose execution the watcher will find "drifted"
# ---------------------------------------------------------------------------


@pytest.fixture
def drifted_server(tmp_path, monkeypatch):
    """A hermetic server whose execution status can be drifted to ``stopped``.

    Same wiring as ``tests/unit/test_executor_watcher_handoff.py``'s
    ``isolated_server`` (imported helpers, same neutral seams), plus the
    knobs this test needs: the pending count and the ``tasks.json``
    payload are both under the test's control.
    """
    import server

    plan_id = "durable-handoff-plan"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "1", "title": "t", "depends_on": []}]})
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    db_path = _make_state_db(tmp_path)
    _seed_plan(db_path, plan_id, project_dir)

    monkeypatch.setattr(server, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    monkeypatch.setattr(server, "_reject_if_placeholder_prd", lambda *a, **k: None)
    monkeypatch.setattr(
        "state_machine.db.archive_scan.classify_plan", lambda *a, **k: "active"
    )
    # Every task terminal → the clean success path.
    monkeypatch.setattr(
        server,
        "_count_unfinished_tasks",
        lambda *a, **k: {"total": 1, "pending_or_unstarted": 0, "failed": 0},
    )
    fake = _FakeProcess()
    monkeypatch.setattr(
        server,
        "_spawn_executor_subprocess",
        lambda **kwargs: (fake, tmp_path / "executor.log"),
    )

    calls: list[tuple] = []
    monkeypatch.setattr(
        server,
        "_run_auto_verification_loop",
        lambda *a, **k: calls.append((a, k)),
    )

    server._execution_state.pop(plan_id, None)
    yield server, plan_id, plan_dir, project_dir, calls, fake
    server._execution_state.pop(plan_id, None)


def _start_and_drift(server, plan_id, project_dir, fake, status="stopped"):
    """Start the execution, then drift its in-memory status.

    The watcher parks on ``process.wait()``; the status is written
    *before* the fake is released, so the watcher is guaranteed to read
    the drifted value when it wakes up — exactly what the 2026-11-03
    incident did to a live run.
    """
    server.start_execution(
        plan_id,
        server.StartExecutionRequest(project_dir=str(project_dir)),
    )
    server._execution_state[plan_id]["status"] = status
    fake.returncode = 0


# ---------------------------------------------------------------------------
# 1. The hand-off fires despite the status drift
# ---------------------------------------------------------------------------


def test_handoff_fires_when_status_drifted_and_exit_is_zero(
    drifted_server, reclaim_workers
):
    """``stopped`` + rc=0 + nothing pending → verification still runs.

    Before the fix the watcher keyed the whole completion branch on
    ``state["status"] == "running"``; a drifted status therefore skipped
    the hand-off *and* left no trace in the log.
    """
    server, plan_id, plan_dir, project_dir, calls, fake = drifted_server

    _start_and_drift(server, plan_id, project_dir, fake, status="stopped")
    calls = _release_and_join(fake, calls, reclaim_workers)

    assert len(calls) == 1, (
        "the auto-verification hand-off did not fire exactly once for a "
        f"drifted run (calls={calls!r})"
    )
    assert server._execution_state[plan_id]["status"] == "completed", (
        "a completed run must read 'completed' after the hand-off"
    )


# ---------------------------------------------------------------------------
# 2. The boundaries — the drift alone is not enough
# ---------------------------------------------------------------------------


def test_no_handoff_when_drifted_and_exit_is_nonzero(
    drifted_server, reclaim_workers, monkeypatch
):
    """Drifted + rc != 0 → no hand-off (the run failed, not finished)."""
    server, plan_id, plan_dir, project_dir, calls, fake = drifted_server

    _start_and_drift(server, plan_id, project_dir, fake, status="stopped")
    fake.returncode = 1
    calls = _release_and_join(fake, calls, reclaim_workers)

    assert calls == [], (
        f"a failed run must not hand off to verification (calls={calls!r})"
    )


def test_no_handoff_when_drifted_and_tasks_still_pending(
    drifted_server, reclaim_workers, monkeypatch
):
    """Drifted + rc=0 + work still pending → no hand-off.

    A half-finished plan must not be verified as if it were done; that
    is the original 2026-08-24 defect this whole branch guards against.
    """
    import server as server_module

    server, plan_id, plan_dir, project_dir, calls, fake = drifted_server

    monkeypatch.setattr(
        server_module,
        "_count_unfinished_tasks",
        lambda *a, **k: {"total": 2, "pending_or_unstarted": 1, "failed": 0},
    )

    _start_and_drift(server, plan_id, project_dir, fake, status="stopped")
    calls = _release_and_join(fake, calls, reclaim_workers)

    assert calls == [], (
        "a plan with pending tasks must not hand off to verification "
        f"(calls={calls!r})"
    )


def test_no_handoff_when_drifted_and_quota_marker_present(
    drifted_server, reclaim_workers
):
    """Drifted + rc=0 + a ``waiting_for_quota`` marker → no hand-off.

    A parked plan is paused, not finished: handing it to verification
    would verify an unfinished plan.
    """
    server, plan_id, plan_dir, project_dir, calls, fake = drifted_server

    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [{"id": "1", "title": "t", "depends_on": []}],
                "stop_reason": "waiting_for_quota",
            }
        )
    )

    _start_and_drift(server, plan_id, project_dir, fake, status="stopped")
    calls = _release_and_join(fake, calls, reclaim_workers)

    assert calls == [], (
        "a quota-parked plan must not hand off to verification "
        f"(calls={calls!r})"
    )


def test_handoff_when_tasks_file_cannot_be_read(
    drifted_server, reclaim_workers, monkeypatch
):
    """Drifted + rc=0 + an unreadable ``tasks.json`` → still hands off.

    ``_count_unfinished_tasks`` is defensive by contract: an unreadable
    task file yields all zeros, i.e. "nothing detected as unfinished".
    The gate inherits that contract, so a plan whose ``tasks.json``
    cannot be parsed hands off instead of stranding.
    """
    from verification_loop import (
        _count_unfinished_tasks as real_count_unfinished_tasks,
    )

    server, plan_id, plan_dir, project_dir, calls, fake = drifted_server

    # The real function, not the fixture's stub — the point is its
    # documented "unreadable → zeros" behaviour.
    monkeypatch.setattr(
        server, "_count_unfinished_tasks", real_count_unfinished_tasks
    )
    (plan_dir / "tasks.json").write_text("{ this is not json")

    _start_and_drift(server, plan_id, project_dir, fake, status="stopped")
    calls = _release_and_join(fake, calls, reclaim_workers)

    assert len(calls) == 1, (
        "an unreadable tasks.json must not strand the plan "
        f"(calls={calls!r})"
    )
