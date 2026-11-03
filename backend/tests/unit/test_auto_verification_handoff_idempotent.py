"""The auto-verification hand-off must happen at most once per execution.

Background (2026-11-03)
----------------------
Task 1 re-based the auto-verification hand-off on *durable* facts —
``rc == 0 AND pending_or_unstarted == 0 AND no quota marker`` — so that
a drifting in-memory ``state["status"]`` could no longer swallow the
hand-off.  Task 2 added the ``auto_verification_skipped`` log so a run
that does *not* hand off says why.

Both of those are stable facts, and that is exactly the problem: when
the completion branch is entered a **second** time for the same
execution, the executor's exit code is still 0, the plan still has
nothing pending and still carries no quota marker — so every durable
fact still says "hand off", and a **second** verification round starts.

A second round is not harmless.  It re-runs the whole verify/repair
cycle against a plan that has already been handed off, and the second
round's outcome is the one that survives.  The material is explicit:
"the hand-off may happen only once; re-entering the watcher must not
start a second verification round."

The fix is a one-shot latch in the run's own in-memory state:
``state['_auto_verification_handoff_started']``.

* it is written **before** the hand-off call, so a verification loop
  that raises cannot be re-entered into a second round;
* it is read back on the next entry, so the second entry skips;
* it lives only in ``_execution_state`` — no new database column, no
  migration.

The latch must not swallow the *other* skip reasons: a run that did not
hand off because the executor crashed, or because work is still
pending, must still say so.  ``already_handed_off`` is only reported
when the latch is the reason the hand-off did not happen.

Contract pinned here
--------------------
* a second entry (latch already set) + ``stopped`` + rc=0 → **no**
  verification round, and exactly one ``auto_verification_skipped`` line
  carrying ``reason=already_handed_off``.
* the first entry → the verification round fires exactly once and the
  latch is set.
* the latch is set but the executor exited non-zero → the reason stays
  ``executor_not_clean_exit`` (no mislabelling).
* the latch is set but work is still pending → the reason stays
  ``pending_tasks`` (no mislabelling).
* the latch is written **before** the verification call → a loop that
  raises still leaves the latch set.

Negative control (run it once by hand, not by pytest)
-----------------------------------------------------
This file must be able to fail without the fix — otherwise it proves
nothing.  With the fix uncommitted (from the repo root):

    git diff backend/routes/execution.py > /tmp/idempotent.patch \
        && git apply -R /tmp/idempotent.patch
    cd backend && <venv>/python3 -m pytest \
        tests/unit/test_auto_verification_handoff_idempotent.py -v
    # → every test in this file MUST FAIL
    cd .. && git apply /tmp/idempotent.patch
    cd backend && <venv>/python3 -m pytest \
        tests/unit/test_auto_verification_handoff_idempotent.py -v
    # → green again

Once the fix is committed, ``git apply -R`` on the *working-tree* diff is
a no-op; capture the diff from the commit instead:

    git show <fix-commit> -- backend/routes/execution.py \
        > /tmp/idempotent.patch
    git apply -R /tmp/idempotent.patch
    # → the tests MUST FAIL
    git apply /tmp/idempotent.patch
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
_LATCH = "_auto_verification_handoff_started"


# ---------------------------------------------------------------------------
# Fixture — a plan whose execution the watcher will find "drifted"
# ---------------------------------------------------------------------------


@pytest.fixture
def idem_server(tmp_path, monkeypatch):
    """A hermetic server whose execution status can be drifted to ``stopped``.

    Same wiring as ``tests/unit/test_executor_watcher_handoff.py``'s
    ``isolated_server`` (imported helpers, same neutral seams), plus the
    knobs this file needs: the pending count and the ``tasks.json``
    payload are both under the test's control.
    """
    import server

    plan_id = "idempotent-handoff-plan"
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
    yield server, plan_id, plan_dir, project_dir, calls, fake
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


# ---------------------------------------------------------------------------
# 1. The second entry must not start a second verification round
# ---------------------------------------------------------------------------


def test_second_entry_does_not_restart_verification(
    idem_server, reclaim_workers, caplog
):
    """The latch is set + ``stopped`` + rc=0 → no second verification round.

    The durable facts are unchanged on the second entry (rc is still 0,
    nothing is pending, no quota marker), so without the latch the
    completion branch fires again and a second verification round
    starts.  The skip must be reported as ``already_handed_off`` so an
    operator can tell "already handed off" from "never handed off".
    """
    server, plan_id, plan_dir, project_dir, calls, fake = idem_server

    with caplog.at_level("WARNING", logger="server"):
        _start(server, plan_id, project_dir, fake, status="stopped", returncode=0)
        # The run already handed off once — the second entry sees the latch.
        server._execution_state[plan_id][_LATCH] = True
        calls = _release_and_join(fake, calls, reclaim_workers)

    assert calls == [], (
        "a second entry must not start another verification round "
        f"(calls={calls!r})"
    )
    assert "reason=already_handed_off" in caplog.text, (
        "the skip must name the latch as the reason: "
        f"{_skip_messages(caplog)!r}"
    )


# ---------------------------------------------------------------------------
# 2. The first entry hands off exactly once and arms the latch
# ---------------------------------------------------------------------------


def test_first_entry_sets_handoff_flag(idem_server, reclaim_workers):
    """A drifted-but-clean first entry → one verification round, latch set.

    The latch is what makes the second entry a no-op, so it has to be
    written on the way *through* the hand-off — not after it.
    """
    server, plan_id, plan_dir, project_dir, calls, fake = idem_server

    _start(server, plan_id, project_dir, fake, status="stopped", returncode=0)
    calls = _release_and_join(fake, calls, reclaim_workers)

    assert len(calls) == 1, (
        "the auto-verification hand-off did not fire exactly once "
        f"(calls={calls!r})"
    )
    assert server._execution_state[plan_id].get(_LATCH) is True, (
        "the hand-off must arm the latch so a second entry can skip it: "
        f"{server._execution_state[plan_id].get(_LATCH)!r}"
    )


# ---------------------------------------------------------------------------
# 3. The latch must not swallow the other skip reasons
# ---------------------------------------------------------------------------


def test_latch_with_nonzero_exit_keeps_exit_reason(
    idem_server, reclaim_workers, caplog
):
    """Latch set + rc != 0 → ``executor_not_clean_exit``, not the latch.

    The latch only explains a skip when the exit was clean and nothing
    was pending; otherwise the run failed for its own reasons and the
    log has to say so.
    """
    server, plan_id, plan_dir, project_dir, calls, fake = idem_server

    with caplog.at_level("WARNING", logger="server"):
        _start(server, plan_id, project_dir, fake, status="stopped", returncode=1)
        server._execution_state[plan_id][_LATCH] = True
        calls = _release_and_join(fake, calls, reclaim_workers)

    msgs = _skip_messages(caplog)
    assert len(msgs) == 1, f"expected exactly one {_EVENT} record, got {msgs!r}"
    assert "reason=executor_not_clean_exit" in msgs[0], msgs[0]
    assert "already_handed_off" not in msgs[0], msgs[0]


def test_latch_with_pending_tasks_keeps_pending_reason(
    idem_server, reclaim_workers, monkeypatch, caplog
):
    """Latch set + rc=0 + work pending → ``pending_tasks``, not the latch."""
    server, plan_id, plan_dir, project_dir, calls, fake = idem_server

    monkeypatch.setattr(
        server,
        "_count_unfinished_tasks",
        lambda *a, **k: {"total": 2, "pending_or_unstarted": 1, "failed": 0},
    )

    with caplog.at_level("WARNING", logger="server"):
        _start(server, plan_id, project_dir, fake, status="stopped", returncode=0)
        server._execution_state[plan_id][_LATCH] = True
        calls = _release_and_join(fake, calls, reclaim_workers)

    assert calls == [], (
        f"a plan with pending tasks must not hand off to verification "
        f"(calls={calls!r})"
    )
    msgs = _skip_messages(caplog)
    assert len(msgs) == 1, f"expected exactly one {_EVENT} record, got {msgs!r}"
    assert "reason=pending_tasks" in msgs[0], msgs[0]
    assert "already_handed_off" not in msgs[0], msgs[0]


# ---------------------------------------------------------------------------
# 4. The latch is written before the call — a raising loop cannot retry
# ---------------------------------------------------------------------------


def test_latch_is_set_before_the_verification_call(
    idem_server, reclaim_workers, monkeypatch
):
    """A verification loop that raises still leaves the latch armed.

    If the latch were written *after* the call, a loop that raised would
    leave the run looking un-handed-off, and the next entry would start
    a second round against the same plan.
    """
    server, plan_id, plan_dir, project_dir, calls, fake = idem_server

    def _boom(*a, **k):
        calls.append((a, k))
        raise RuntimeError("verification loop exploded")

    monkeypatch.setattr(server, "_run_auto_verification_loop", _boom)

    _start(server, plan_id, project_dir, fake, status="stopped", returncode=0)
    _release_and_join(fake, calls, reclaim_workers)

    assert len(calls) == 1, (
        f"the auto-verification hand-off did not fire exactly once "
        f"(calls={calls!r})"
    )
    assert server._execution_state[plan_id].get(_LATCH) is True, (
        "the latch must be armed before the verification call, so a raising "
        "loop cannot be retried into a second round: "
        f"{server._execution_state[plan_id].get(_LATCH)!r}"
    )
