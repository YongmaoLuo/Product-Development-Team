"""Integration acceptance: the hand-off, end to end, on real durable facts.

Background (2026-11-03)
----------------------
The unit suites that came with this change (``test_auto_verification_
handoff_durable.py``, ``..._skip_log.py``, ``..._preserved.py``,
``..._idempotent.py``) all **stub** the pending counter:

    monkeypatch.setattr(
        server,
        "_count_unfinished_tasks",
        lambda *a, **k: {"total": 1, "pending_or_unstarted": 0, "failed": 0},
    )

That is the right seam for "what does the watcher do when the counter
says N" -- but it means none of those suites ever proves that the
counter's *real* answer reaches the hand-off gate.  A wiring mistake
between the runtime overlay (``plan_tasks``) and the counter would leave
every one of them green: the stub answers the question, so the real
answer is never asked.

This file is the acceptance layer that asks the real question.  Nothing
about the pending count is stubbed:

* the counter is the production ``_count_unfinished_tasks``;
* the static task list is a real ``tasks.json`` on disk;
* the runtime overlay is a real ``plan_tasks`` row, written through
  ``PlanTaskRepository.update_task`` and committed.

Contract pinned here
--------------------
* overlay says the task is ``completed`` -> the counter reports nothing
  pending -> a drifted run (status ``stopped``) + ``rc == 0`` hands off
  to auto-verification **exactly once**;
* overlay has **no row** for the task -> the counter reports it as
  never started -> **no hand-off**, and the log says why
  (``auto_verification_skipped`` / ``reason=pending_tasks``).

Negative control (run it once by hand, not by pytest)
-----------------------------------------------------
This file must be able to fail without the fix -- otherwise it proves
nothing.  Both windows below were run by hand for this change; the
"uncommitted fix" window is the one the task brief prescribes.

**While the fix is still uncommitted** (from the repo root):

    git diff backend/routes/execution.py > /tmp/handoff-integration.patch \
        && git checkout -- backend/routes/execution.py
    cd backend && <venv>/python3 -m pytest \
        tests/unit/test_auto_verification_handoff_integration.py -v
    # -> test_handoff_fires_when_the_real_counter_reports_nothing_pending
    #    MUST FAIL
    cd .. && git apply /tmp/handoff-integration.patch
    cd backend && <venv>/python3 -m pytest \
        tests/unit/test_auto_verification_handoff_integration.py -v
    # -> green again

``git checkout --`` reverts the *whole file* to its committed state, so
it discards every other uncommitted change in that file too -- use it
only when the fix is that file's only uncommitted change.  The
equivalent that touches nothing but the recorded diff is:

    git diff backend/routes/execution.py > /tmp/handoff-integration.patch \
        && git apply -R /tmp/handoff-integration.patch

**Once the fix is committed** -- ``git checkout --`` would then restore
the *fixed* file (HEAD carries the fix), so the "revert" would be a
no-op that still reports green.  Capture the diff from the commit that
introduced the fix instead:

    git show <fix-commit> -- backend/routes/execution.py \
        > /tmp/handoff-integration.patch
    git apply -R /tmp/handoff-integration.patch
    cd backend && <venv>/python3 -m pytest \
        tests/unit/test_auto_verification_handoff_integration.py -v
    # -> test_handoff_fires_when_the_real_counter_reports_nothing_pending
    #    MUST FAIL
    cd .. && git apply /tmp/handoff-integration.patch
    cd backend && <venv>/python3 -m pytest \
        tests/unit/test_auto_verification_handoff_integration.py -v
    # -> green again

``git apply -R`` / ``git apply`` touches only the recorded diff, so it
never throws away another writer's uncommitted change in that file --
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


_EVENT = "auto_verification_skipped"


# ---------------------------------------------------------------------------
# Fixture — a hermetic plan whose pending count is REAL
# ---------------------------------------------------------------------------


@pytest.fixture
def integration_server(tmp_path, monkeypatch):
    """A hermetic server whose pending count is computed, never stubbed.

    Same wiring as ``tests/unit/test_executor_watcher_handoff.py``'s
    ``isolated_server`` -- but that fixture stubs
    ``_count_unfinished_tasks``, which is exactly the thing this file
    exists to exercise.  Everything else is identical; the counter is
    left alone.

    The fixture yields the ``state.db`` path too, so a case can write the
    runtime overlay (``plan_tasks``) the way the executor would.
    """
    import server

    plan_id = "integration-handoff-plan"
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
    # ``_count_unfinished_tasks`` is deliberately NOT stubbed: this file
    # is the one that asks the real counter.

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
    yield server, plan_id, plan_dir, project_dir, calls, fake, db_path
    server._execution_state.pop(plan_id, None)


def _write_overlay(db_path, plan_id, task_id, status) -> None:
    """Write the runtime overlay row the executor would have written."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        PlanTaskRepository(conn).update_task(
            plan_id, task_id, {"status": status}
        )
        conn.commit()
    finally:
        conn.close()


def _start_and_drift(server, plan_id, project_dir, fake, status="stopped"):
    """Start the execution, then drift its in-memory status.

    The watcher parks on ``process.wait()``; the status is written
    *before* the fake is released, so the watcher is guaranteed to read
    the drifted value when it wakes up -- exactly what the 2026-11-03
    incident did to a live run.
    """
    server.start_execution(
        plan_id,
        server.StartExecutionRequest(project_dir=str(project_dir)),
    )
    server._execution_state[plan_id]["status"] = status
    fake.returncode = 0


def _skip_messages(caplog) -> list[str]:
    """The ``auto_verification_skipped`` messages captured so far."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith(_EVENT)
    ]


# ---------------------------------------------------------------------------
# 1. The real counter says "nothing pending" -> the hand-off fires
# ---------------------------------------------------------------------------


def test_handoff_fires_when_the_real_counter_reports_nothing_pending(
    integration_server, reclaim_workers
):
    """Overlay says ``completed`` -> the hand-off fires exactly once.

    Nothing about the pending count is stubbed here: ``tasks.json`` says
    the plan has one task, the runtime overlay says that task is
    ``completed``, and the production ``_count_unfinished_tasks``
    cross-checks the two and reports nothing pending.  A drifted status
    (``stopped``) plus ``rc == 0`` must therefore still hand off.

    Before the fix this case was red: the whole completion branch was
    gated on ``state["status"] == "running"``, so the drifted run never
    reached the hand-off.
    """
    server, plan_id, plan_dir, project_dir, calls, fake, db_path = (
        integration_server
    )

    _write_overlay(db_path, plan_id, "1", "completed")

    _start_and_drift(server, plan_id, project_dir, fake, status="stopped")
    calls = _release_and_join(fake, calls, reclaim_workers)

    assert len(calls) == 1, (
        "the auto-verification hand-off must fire exactly once for a "
        f"drifted run whose real pending count is zero (calls={calls!r})"
    )


# ---------------------------------------------------------------------------
# 2. The real counter says "one task never started" -> no hand-off
# ---------------------------------------------------------------------------


def test_no_handoff_when_the_real_counter_reports_a_pending_task(
    integration_server, reclaim_workers, caplog
):
    """No overlay row -> the task counts as never started -> no hand-off.

    This is the mirror image of case 1 and it is the case that would be
    missed by a stubbed counter: the stub in the other suites always
    answers "nothing pending", so nothing in them proves that a *really*
    pending task keeps the plan out of verification.  Here the overlay
    has no row for task ``1`` at all, so the production counter counts
    it as never started, and the run must not hand off -- and must say
    so in the log.
    """
    server, plan_id, plan_dir, project_dir, calls, fake, db_path = (
        integration_server
    )

    # Deliberately NO overlay row is written: task "1" is in
    # ``tasks.json`` but the executor never picked it up.
    _start_and_drift(server, plan_id, project_dir, fake, status="stopped")
    with caplog.at_level("WARNING", logger="server"):
        calls = _release_and_join(fake, calls, reclaim_workers)

    assert calls == [], (
        "a plan with a task that never started must not hand off to "
        f"verification (calls={calls!r})"
    )

    messages = _skip_messages(caplog)
    assert len(messages) == 1, (
        "the skip must leave exactly one structured trace "
        f"(messages={messages!r})"
    )
    assert "reason=pending_tasks" in messages[0], (
        f"the skip must name the durable fact that ruled it out "
        f"(message={messages[0]!r})"
    )


# ---------------------------------------------------------------------------
# 3. The negative-control recipe stays documented
# ---------------------------------------------------------------------------


def test_negative_control_recipe_is_documented():
    """The recipe that makes this file red before the fix must stay here.

    A negative-control recipe that lives only in a commit message is
    gone the moment the branch is squashed -- and then nobody can check
    that the file still fails without the fix.  The recipe therefore
    lives in this module's docstring, and this case pins the two
    windows it has to cover: the ``git apply -R`` one and the
    ``git checkout --`` one.
    """
    doc = __doc__ or ""

    assert "git apply -R" in doc, (
        "the docstring must carry the 'git apply -R' negative-control "
        "window"
    )
    assert "git checkout --" in doc, (
        "the docstring must carry the 'git checkout --' negative-control "
        "window"
    )
    assert (
        "test_handoff_fires_when_the_real_counter_reports_nothing_pending"
        in doc
    ), (
        "the docstring must name the case that goes red without the fix"
    )
