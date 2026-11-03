"""A skipped auto-verification hand-off must leave a structured trace.

Background (2026-11-03)
----------------------
Task 1 re-based the auto-verification hand-off on *durable* facts
(``rc``, the pending count, the quota marker) instead of the mutable
in-memory ``state["status"]``.  But the branch it introduced still has a
silent hole: when the durable facts say "this run did not finish" the
watcher simply falls through the ``else`` and says nothing at all.

That is exactly what an operator cannot use.  When a plan stops without
a verification round there is no line in the log that says *why* — the
run looks identical to a plan that was never started.

Contract pinned here
--------------------
Every skip path emits exactly one ``auto_verification_skipped`` warning
carrying the fields the decision was made on:

* ``plan``       — which plan was skipped
* ``reason``     — which of the skip reasons applied
* ``rc``         — the executor's exit code
* ``pending_or_unstarted`` — the pending count the decision used
* ``status``     — the in-memory status at the moment of the decision

The reasons are, in decision order:

* ``executor_not_clean_exit`` — the executor did not exit 0
* ``pending_tasks``           — work is still pending
* ``waiting_for_quota``       — the plan is parked for quota
* ``status_not_running``      — the fallback

The log is an *observation*, never a state change: it must not move the
in-memory status, must not move the database row, and must not fire on
the paths that do hand off.

Negative control (run it once by hand, not by pytest)
-----------------------------------------------------
This file must be able to fail without the fix — otherwise it proves
nothing.  With the fix uncommitted (from the repo root):

    git diff backend/routes/execution.py > /tmp/skiplog.patch \
        && git apply -R /tmp/skiplog.patch
    cd backend && <venv>/python3 -m pytest \
        tests/unit/test_auto_verification_handoff_skip_log.py -v
    # → every test in this file MUST FAIL
    cd .. && git apply /tmp/skiplog.patch
    cd backend && <venv>/python3 -m pytest \
        tests/unit/test_auto_verification_handoff_skip_log.py -v
    # → green again

Once the fix is committed, ``git apply -R`` on the *working-tree* diff is
a no-op; capture the diff from the commit instead:

    git show <fix-commit> -- backend/routes/execution.py \
        > /tmp/skiplog.patch
    git apply -R /tmp/skiplog.patch
    # → the tests MUST FAIL
    git apply /tmp/skiplog.patch
    # → green again
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
# Fixture — a plan whose execution the watcher will find "drifted"
# ---------------------------------------------------------------------------


@pytest.fixture
def skip_server(tmp_path, monkeypatch):
    """A hermetic server whose execution status can be drifted to ``stopped``.

    Same wiring as ``tests/unit/test_executor_watcher_handoff.py``'s
    ``isolated_server`` (imported helpers, same neutral seams), plus the
    knobs this file needs: the pending count and the ``tasks.json``
    payload are both under the test's control.
    """
    import server

    plan_id = "skip-log-plan"
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
    # Every task terminal → nothing pending, no quota marker.
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
    yield server, plan_id, plan_dir, project_dir, calls, fake, db_path
    server._execution_state.pop(plan_id, None)


def _start(server, plan_id, project_dir, fake, status="stopped", returncode=0):
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
    fake.returncode = returncode


def _skip_messages(caplog) -> list[str]:
    """The ``auto_verification_skipped`` messages captured so far."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith(_EVENT)
    ]


def _exec_status(db_path, plan_id) -> str | None:
    """The persisted ``plan_execution.exec_status`` for the plan."""
    from state_machine.db.connection import open as open_db

    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT exec_status FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


# ---------------------------------------------------------------------------
# 1. The drifted + non-zero exit — the case the material names first
# ---------------------------------------------------------------------------


def test_skip_log_records_reason_when_status_drifted_and_non_zero_exit(
    skip_server, reclaim_workers, caplog
):
    """``stopped`` + rc=1 → one ``auto_verification_skipped`` line.

    Before the fix the watcher fell through the ``else`` in silence: a
    run that neither finished nor explained itself left no trace.
    """
    server, plan_id, plan_dir, project_dir, calls, fake, db_path = skip_server

    with caplog.at_level("WARNING", logger="server"):
        _start(server, plan_id, project_dir, fake, status="stopped", returncode=1)
        calls = _release_and_join(fake, calls, reclaim_workers)

    msgs = _skip_messages(caplog)
    assert len(msgs) == 1, (
        f"expected exactly one {_EVENT} record, got {msgs!r}"
    )
    assert "reason=executor_not_clean_exit" in msgs[0], msgs[0]
    assert "rc=1" in msgs[0], msgs[0]
    assert "status=stopped" in msgs[0], msgs[0]
    assert f"plan={plan_id}" in msgs[0], msgs[0]


# ---------------------------------------------------------------------------
# 2. The pending count
# ---------------------------------------------------------------------------


def test_skip_log_records_pending_count_when_tasks_remain(
    skip_server, reclaim_workers, monkeypatch, caplog
):
    """``stopped`` + rc=0 + work still pending → ``reason=pending_tasks``.

    The pending count is the field the decision was made on, so it has
    to be on the line — otherwise the operator cannot tell "one task
    left" from "everything is left".
    """
    server, plan_id, plan_dir, project_dir, calls, fake, db_path = skip_server

    monkeypatch.setattr(
        server,
        "_count_unfinished_tasks",
        lambda *a, **k: {"total": 2, "pending_or_unstarted": 1, "failed": 0},
    )

    with caplog.at_level("WARNING", logger="server"):
        _start(server, plan_id, project_dir, fake, status="stopped", returncode=0)
        calls = _release_and_join(fake, calls, reclaim_workers)

    msgs = _skip_messages(caplog)
    assert len(msgs) == 1, (
        f"expected exactly one {_EVENT} record, got {msgs!r}"
    )
    assert "reason=pending_tasks" in msgs[0], msgs[0]
    assert "pending_or_unstarted=1" in msgs[0], msgs[0]
    assert "rc=0" in msgs[0], msgs[0]


# ---------------------------------------------------------------------------
# 3. The quota marker
# ---------------------------------------------------------------------------


def test_skip_log_records_quota_marker(skip_server, reclaim_workers, caplog):
    """A ``waiting_for_quota`` marker → ``reason=waiting_for_quota``.

    A parked plan is paused, not finished; the reason has to say so
    explicitly, because "the executor exited 0" alone reads like a
    success.
    """
    server, plan_id, plan_dir, project_dir, calls, fake, db_path = skip_server

    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [{"id": "1", "title": "t", "depends_on": []}],
                "stop_reason": "waiting_for_quota",
            }
        )
    )

    with caplog.at_level("WARNING", logger="server"):
        _start(server, plan_id, project_dir, fake, status="stopped", returncode=0)
        calls = _release_and_join(fake, calls, reclaim_workers)

    msgs = _skip_messages(caplog)
    assert len(msgs) == 1, (
        f"expected exactly one {_EVENT} record, got {msgs!r}"
    )
    assert "reason=waiting_for_quota" in msgs[0], msgs[0]


# ---------------------------------------------------------------------------
# 4. The log is an observation, never a state change
# ---------------------------------------------------------------------------


def test_skip_log_does_not_change_state_or_db(
    skip_server, reclaim_workers, caplog
):
    """A skipped hand-off must leave the state and the DB exactly as they were.

    The skip path is not a disposition: it neither completes nor fails
    the plan.  If logging moved the status, the log would be deciding
    the outcome — which is what the durable-fact gate exists to avoid.
    """
    server, plan_id, plan_dir, project_dir, calls, fake, db_path = skip_server

    with caplog.at_level("WARNING", logger="server"):
        _start(server, plan_id, project_dir, fake, status="stopped", returncode=1)
        calls = _release_and_join(fake, calls, reclaim_workers)

    assert len(_skip_messages(caplog)) == 1, (
        "the skip path must still log exactly once"
    )
    assert server._execution_state[plan_id]["status"] == "stopped", (
        "the skip log must not move the in-memory status: "
        f"{server._execution_state[plan_id].get('status')!r}"
    )
    assert _exec_status(db_path, plan_id) == "stopped", (
        "the skip log must not move the persisted status: "
        f"{_exec_status(db_path, plan_id)!r}"
    )


# ---------------------------------------------------------------------------
# 5. A non-zero exit has no pending count — the line must still be written
# ---------------------------------------------------------------------------


def test_skip_log_records_none_pending_when_exit_was_not_clean(
    skip_server, reclaim_workers, caplog
):
    """rc != 0 → the pending count was never read → the field says ``None``.

    ``_unfinished_at_exit`` is only computed for a clean exit, so the
    field is genuinely unknown on this path.  Writing ``None`` is the
    honest answer; formatting an absent number must not raise.
    """
    server, plan_id, plan_dir, project_dir, calls, fake, db_path = skip_server

    with caplog.at_level("WARNING", logger="server"):
        _start(server, plan_id, project_dir, fake, status="stopped", returncode=3)
        calls = _release_and_join(fake, calls, reclaim_workers)

    msgs = _skip_messages(caplog)
    assert len(msgs) == 1, (
        f"expected exactly one {_EVENT} record, got {msgs!r}"
    )
    assert "pending_or_unstarted=None" in msgs[0], msgs[0]
    assert "rc=3" in msgs[0], msgs[0]


# ---------------------------------------------------------------------------
# 6. The paths that DO hand off stay quiet
# ---------------------------------------------------------------------------


def test_no_skip_log_when_handoff_fires(skip_server, reclaim_workers, caplog):
    """A run that hands off must not also claim to have skipped.

    The hand-off path is unchanged: it keeps its own log lines and adds
    no ``auto_verification_skipped`` record.
    """
    server, plan_id, plan_dir, project_dir, calls, fake, db_path = skip_server

    with caplog.at_level("WARNING", logger="server"):
        _start(server, plan_id, project_dir, fake, status="running", returncode=0)
        calls = _release_and_join(fake, calls, reclaim_workers)

    assert len(calls) == 1, (
        f"the auto-verification hand-off did not fire exactly once (calls={calls!r})"
    )
    assert _skip_messages(caplog) == [], (
        "a run that hands off to verification must not log a skip: "
        f"{_skip_messages(caplog)!r}"
    )