"""The hand-off fix must not have changed the dispositions around it.

Background (2026-11-03)
-----------------------
Two earlier changes re-based the auto-verification hand-off gate on
durable facts instead of on the mutable in-memory ``state["status"]``:

* task 1 — the gate reads ``rc == 0 AND pending_or_unstarted == 0 AND
  no quota marker`` (see ``routes/execution.py``), so a status that
  drifted to ``"stopped"`` mid-run no longer swallows the hand-off;
* task 2 — a hand-off that does not happen is now *said out loud* in
  the log (``auto_verification_skipped``).

Both changes sit right next to three dispositions that already existed
and that nobody asked to be touched:

* a **non-zero exit** still takes the original failure disposition;
* **rc == 0 with work still pending** still fails the plan — it does
  not hand off to verification;
* the **quota disposition still comes first**: a plan parked for quota
  is parked, not failed, and not verified.

This file pins those three.  They are deliberately *not* cases that the
fix had to change: they are green before the fix and green after it.
Their job is to catch an **over-reach** — a fix that quietly rewrote a
disposition nobody asked it to rewrite.  A regression here would read
as "the fix broke the plan's failure handling", and nothing in the
hand-off's own test file would notice.

Contract pinned here
--------------------
* drifted status (``stopped``) + rc != 0 → no hand-off, and the status
  stays ``stopped`` — the drift is an observation, not a disposition.
* ``running`` + rc == 0 + work pending → no hand-off; the plan is
  ``failed`` with ``executor_exited_with_unfinished_tasks``.
* ``running`` + rc == 0 + work pending + a ``waiting_for_quota``
  marker → no hand-off; the plan is parked in ``waiting_for_quota``.

None of the three may call ``_run_auto_verification_loop``.

Negative control (run it by hand, not by pytest)
------------------------------------------------
A regression test that cannot fail proves nothing, and these three
were written against a *fixed* tree — so "it passes" is the expected
result and cannot be the evidence.  The evidence is that each case
fails when the pinned disposition is broken.  All three mutations below
were run against this file on 2026-11-03; each one was caught by its
case, and the file was restored byte-for-byte afterwards.  From the
repo root:

    # 1. drifted + rc=1 must not be rewritten to "failed".
    #    In ``backend/routes/execution.py`` change
    #        if _status_at_exit == "running" or _durable_completion:
    #    to
    #        if True:
    #    → test_no_handoff_when_status_drifted_and_non_zero_exit FAILS
    #       with  assert 'failed' == 'stopped'

    # 2. rc=0 + pending must fail the plan, not hand off.
    #    Change
    #        if unfinished["pending_or_unstarted"] > 0:
    #    to
    #        if False:
    #    → test_no_handoff_and_failed_when_tasks_still_pending FAILS
    #       with  assert [...] == []   (the hand-off fired)

    # 3. the quota disposition must come FIRST.
    #    Change
    #        if _enter_quota_wait(
    #            plan_id, plan_dir, state, tasks_file,
    #        ):
    #    to
    #        if False and _enter_quota_wait(
    #            plan_id, plan_dir, state, tasks_file,
    #        ):
    #    → test_quota_marker_still_parks_plan FAILS
    #       with  assert 'failed' == 'waiting_for_quota'

``git apply -R`` / ``git apply`` touch only the recorded diff, so they
never throw away another writer's uncommitted change in that file.
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
# The stub PlanState
# ---------------------------------------------------------------------------


class _StubPlanState:
    """A ``PlanState`` that records nothing and refuses nothing.

    The watcher reaches ``PlanState`` on three of its dispositions
    (park for quota, fail with work pending, fail on a non-zero exit),
    and the real class would try to move the plan through its own
    transition table — a side effect this file is not about, and one
    that raises ``ValueError`` for a phase the seeded plan never
    reached.  Both ``transition_to`` and ``force_set_phase`` are
    therefore no-ops: the stub must answer both, because the quota
    disposition falls back from the first to the second when the first
    raises ``ValueError``.

    It is installed only *after* ``start_execution`` has returned —
    ``start_execution`` itself transitions the plan into ``executing``
    and needs the real class.

    ``get_current_phase`` is not one of the two methods the disposition
    branches need, but the watcher's post-hand-off settle path calls it
    (``_settle_phase_after_verification_loop``).  A stub that raises
    ``AttributeError`` there would be swallowed by the watcher's own
    ``except Exception`` and the crash handler would rewrite the status
    — blurring the very distinction these cases pin.  Answering it
    keeps a regression's failure signal about the disposition instead
    of about the stub.
    """

    def __init__(self, *a, **k):
        pass

    def transition_to(self, *a, **k):
        pass

    def force_set_phase(self, *a, **k):
        pass

    def get_current_phase(self, *a, **k):
        return "executing"


# ---------------------------------------------------------------------------
# Fixture — a plan whose execution the watcher will finish
# ---------------------------------------------------------------------------


@pytest.fixture
def watcher_server(tmp_path, monkeypatch):
    """A hermetic server with a parked executor and a recorded hand-off.

    Same wiring as ``tests/unit/test_executor_watcher_handoff.py``'s
    ``isolated_server`` (its helpers are imported, not re-derived), with
    two differences that matter here:

    * ``_count_unfinished_tasks`` is **not** stubbed in the fixture —
      every case below says for itself how many tasks are pending;
    * the seeded ``tasks.json`` carries one task (``id="1"``) that has
      **no row in the runtime overlay**, so the watcher derives an
      unfinished id from disk rather than inheriting one from a stub.
    """
    import server

    plan_id = "preserved-handoff-plan"
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


def _start(server, plan_id, project_dir, monkeypatch):
    """Start the execution, then swap ``PlanState`` for the no-op stub.

    ``start_execution`` needs the REAL ``PlanState`` (it transitions the
    plan into ``executing``), so the stub can only go in once the call
    has returned.  The watcher thread parks on ``process.wait()`` until
    the fake is released, so the swap always lands before the watcher
    reaches any ``PlanState`` call.
    """
    server.start_execution(
        plan_id,
        server.StartExecutionRequest(project_dir=str(project_dir)),
    )
    monkeypatch.setattr(server, "PlanState", _StubPlanState)


# ---------------------------------------------------------------------------
# 1. A non-zero exit still takes the original disposition
# ---------------------------------------------------------------------------


def test_no_handoff_when_status_drifted_and_non_zero_exit(
    watcher_server, monkeypatch, reclaim_workers
):
    """Drifted status + rc != 0 → no hand-off, and the status is left alone.

    The status drifted to ``"stopped"`` while the executor was still
    alive (that is the 2026-11-03 incident), and the subprocess then
    exited non-zero.  The run failed — that is the disposition the
    branch has always had — and the drift is an *observation*: it must
    not be rewritten into ``failed`` / ``completed`` on the way out.
    """
    server, plan_id, plan_dir, project_dir, calls, fake = watcher_server

    _start(server, plan_id, project_dir, monkeypatch)
    server._execution_state[plan_id]["status"] = "stopped"
    fake.returncode = 1

    calls = _release_and_join(fake, calls, reclaim_workers)

    assert calls == [], (
        f"a non-zero exit must not hand off to verification (calls={calls!r})"
    )
    assert server._execution_state[plan_id]["status"] == "stopped", (
        "the drifted status is an observation, not a disposition — it must "
        "not be rewritten to failed / completed"
    )


# ---------------------------------------------------------------------------
# 2. rc=0 with work still pending still fails the plan
# ---------------------------------------------------------------------------


def test_no_handoff_and_failed_when_tasks_still_pending(
    watcher_server, monkeypatch, reclaim_workers
):
    """``running`` + rc=0 + work pending → no hand-off, the plan fails.

    A half-finished plan must not be verified as if it were done: the
    verification round would report tasks that never started as "all
    done" and would run against the completed subset only.  The
    disposition is ``failed`` with
    ``executor_exited_with_unfinished_tasks:<n>``.

    ``_are_all_unfinished_blocked_by_failed_upstream`` is stubbed to
    ``False`` on purpose — that is the "the executor crashed before it
    ran this task" case, which is a real failure rather than the
    partial-completion case (where every unfinished task is blocked by
    a failed upstream and the plan is completed on its finished subset).
    """
    server, plan_id, plan_dir, project_dir, calls, fake = watcher_server

    monkeypatch.setattr(
        server,
        "_count_unfinished_tasks",
        lambda *a, **k: {"total": 2, "pending_or_unstarted": 1, "failed": 0},
    )
    blocked_calls: list[list] = []
    monkeypatch.setattr(
        server,
        "_are_all_unfinished_blocked_by_failed_upstream",
        lambda *a, **k: blocked_calls.append(list(a)) or False,
    )

    _start(server, plan_id, project_dir, monkeypatch)
    fake.returncode = 0

    calls = _release_and_join(fake, calls, reclaim_workers)

    assert calls == [], (
        "a plan with pending tasks must not hand off to verification "
        f"(calls={calls!r})"
    )
    assert server._execution_state[plan_id]["status"] == "failed", (
        "rc=0 with work still pending is a failure, not a completion "
        f"(status={server._execution_state[plan_id]['status']!r})"
    )
    assert server._execution_state[plan_id]["stop_reason"].startswith(
        "executor_exited_with_unfinished_tasks"
    ), (
        "the failure must say why: "
        f"{server._execution_state[plan_id]['stop_reason']!r}"
    )
    # The unfinished ids must have been DERIVED, not inherited from a
    # stub: the task id in ``tasks.json`` has no row in the runtime
    # overlay, so the watcher reads it off disk.  An empty list here
    # would mean the derivation raised and was swallowed.
    assert blocked_calls and blocked_calls[0][1] == ["1"], (
        "the unfinished ids must be derived from tasks.json — the task id "
        f"has no overlay row (got {blocked_calls!r})"
    )


# ---------------------------------------------------------------------------
# 3. The quota disposition still comes first
# ---------------------------------------------------------------------------


def test_quota_marker_still_parks_plan(
    watcher_server, monkeypatch, reclaim_workers
):
    """``running`` + rc=0 + pending + a quota marker → the plan is parked.

    The quota disposition is checked BEFORE the blocked-only branch and
    well before the failed branch: a plan parked because every provider
    had no capacity has no failed task and no blocked task — its tasks
    are simply still eligible for another dispatch.  Parking it is what
    keeps the plan recoverable; failing it would strand work that a
    later dispatch can still pick up.

    ``_are_all_unfinished_blocked_by_failed_upstream`` is stubbed to
    ``False`` on purpose: it is the value that would send the plan down
    the *failed* branch if the quota check were not reached first.  With
    it stubbed, "waiting_for_quota" can only be the result of the quota
    branch returning first — which is the ordering this case pins.
    """
    server, plan_id, plan_dir, project_dir, calls, fake = watcher_server

    monkeypatch.setattr(
        server,
        "_count_unfinished_tasks",
        lambda *a, **k: {"total": 2, "pending_or_unstarted": 1, "failed": 0},
    )
    blocked_calls: list[list] = []
    monkeypatch.setattr(
        server,
        "_are_all_unfinished_blocked_by_failed_upstream",
        lambda *a, **k: blocked_calls.append(list(a)) or False,
    )
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [{"id": "1", "title": "t", "depends_on": []}],
                "stop_reason": "waiting_for_quota",
            }
        )
    )

    _start(server, plan_id, project_dir, monkeypatch)
    fake.returncode = 0

    calls = _release_and_join(fake, calls, reclaim_workers)

    assert calls == [], (
        "a quota-parked plan must not hand off to verification "
        f"(calls={calls!r})"
    )
    assert server._execution_state[plan_id]["status"] == "waiting_for_quota", (
        "the quota disposition comes first — the plan is parked, not failed "
        f"(status={server._execution_state[plan_id]['status']!r})"
    )
    # The ordering itself: the blocked-only branch must not even be
    # consulted.  It is the branch that would fail the plan, so "it was
    # never called" IS the proof that the quota disposition returned
    # first — a status assertion alone could be satisfied by a plan that
    # happened to land on the right value.
    assert blocked_calls == [], (
        "the quota disposition comes first — the blocked-only branch must "
        f"not be consulted at all (got {blocked_calls!r})"
    )
