"""HTTP contract: a quota-parked plan still answers the progress endpoint.

The criterion this file pins
----------------------------
The import acceptance criterion for the capacity-wait work reads:

    *the downstream tasks stay pending — they are neither judged dead nor
    skipped*

It was checked twice by hand, with one-shot probes against the shared
port::

    GET /api/execution/20261024-vp-quota-parked/progress  ->  connection refused
    GET /api/execution/20261024-vp-quota-parked/progress  ->  404
        "Execution not configured for this plan — call
         /api/execution/{plan_id}/start first"

Neither answer is the criterion's.  The first one is about the
*service's* lifetime — the backend had been recycled before the request
was made, so it said nothing about the plan.  The second one is about
an *empty database* the probe happened to hit: a plan that has no
``plan_execution`` row there is, of course, a plan that was never
started.  Both are environment facts.  A one-shot probe against a
shared port is not evidence about a plan.

The endpoint's contract was the real gap
----------------------------------------
``get_execution_progress`` answered 404 whenever
``_get_project_dir()`` came back empty.  A plan parked on quota has a
readable ``plan_execution`` row and may carry no project directory at
all, so the one question an operator asks about a parked plan — "what
is still left to do?" — was answered with "this plan was never started,
call /start first".  That is false: the plan WAS started, it parked,
and its downstream tasks are still waiting for it.

What this file fixes
--------------------
A readable ``plan_execution`` row is the evidence that a plan was
configured for execution.  Only the **absence** of such a row may
produce the 404.  With a row present — ``waiting_for_quota`` included
— the endpoint must answer with the row's ``exec_status`` and the four
per-task counts, read from ``state.db`` and ``tasks.json``.

The three tests are the three cells of that rule:

  * ``test_parked_execution_progress_counts`` — the parked plan, row
    present and project dir present: 200, ``waiting_for_quota``, and
    the counts, with the downstream task still ``pending`` (not
    ``failed``, not ``skipped``).
  * ``test_progress_without_project_dir_still_reports_counts`` — the
    parked plan whose row carries NO project dir: still 200 with the
    same counts, because the row — not the project dir — is what proves
    the plan was started.
  * ``test_no_execution_row_still_404`` — a plan with no execution
    record at all: still 404, with the original message.  This is the
    negative control: the fix must not turn "never started" into
    "started".

The fourth case: the service outlives the starter
--------------------------------------------------
``test_progress_counts_stay_answerable_after_the_round_service_outlives_its_starter``
is the case the counts rule was written for, and it is a different
shape from the three above.

A plan parked on capacity by the process that owns the run leaves
every OTHER process holding a registry that still says ``running`` —
with a pid that has since exited.  The watchdog judges the registry's
status and probes the registry's pid, so it judged the parked plan's
dead pid: the pause was rewritten as a crash, the row was overwritten
with ``failed``, and the interrupted task's ``in_progress`` row was
reaped back to ``pending``.  The plan then reported "nothing is
running and two tasks are still pending" for work that was merely
waiting for capacity.

So the counts have to stay answerable across that boundary: the task
interrupted by the park still reads ``in_progress``, the two tasks
downstream of it are still ``pending``, and nothing has failed.  And
the answer names the instance that produced it — a reader that cannot
say WHICH instance answered, and which ``state.db`` it read, cannot
compare two answers about "the same" plan.

Hermeticity
------------
The plans tree and the state database live under the test's own
``tmp_path`` — the autouse ``isolated_plans_dir`` fixture points
``server.PLANS_DIR`` / ``server._state_db_path`` and the
``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH`` env vars at it — so the
operator's live database and ``plans/`` tree are never touched.  The
rows are written through the real repositories, so the endpoint reads
back the shapes production writes.

The in-memory ``_execution_state`` is cleared on purpose: the row has
to be readable **on its own**.  A server that happens to hold the plan
in memory would answer from memory, and the case this contract is
about — the one after a restart — is exactly the one where memory is
empty.  The fourth case is the one exception: there the registry is
seeded with the STALE view the serving process would really be holding
(a boot-time scan restored the row while it still said ``running``),
because a stale registry is the thing that must not win.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

import server


#: The plan id the failed probes used.  Reusing it keeps the criterion
#: and its evidence about the same plan.
PLAN_ID = "20261024-vp-quota-parked"

#: The three tasks of the parked plan.  ``VP-002`` is the one that ran
#: into the provider's wall; ``VP-003`` is downstream of it and must
#: therefore still be ``pending``.
TASK_IDS = ("VP-001", "VP-002", "VP-003")

#: The one task that finished before the park.
COMPLETED_TASK_ID = "VP-001"

#: The plan of the fourth case.  It is a different plan on purpose:
#: this case is about the process that STARTED the run being gone
#: while the service that answers for the plan is still alive.
SURVIVOR_PLAN_ID = "20261026-vp-quota-parked-survivor"

#: The task that was interrupted by the park — the one the executor
#: was working on when the wall hit.
INTERRUPTED_TASK_ID = "VP-001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    """Return a PID that is guaranteed not to be running.

    A real process is started and waited for, so the number returned is
    one the operating system has already handed out and already taken
    back.  The assertion at the end is what makes that a fact rather
    than a hope: if the PID were somehow alive again, the test says so
    instead of silently probing a stranger's process.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    pid = proc.pid
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return pid
    raise AssertionError(
        f"pid {pid} came back to life — it is not a dead pid, so this "
        f"test would be probing a live process"
    )


def _write_tasks_file(plan_dir: Path) -> None:
    """Write the static task list of the plan.

    Static fields only — ``status`` lives in ``state.db`` (``plan_tasks``)
    since the runtime overlay landed there, and the endpoint overlays it
    onto this list.
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": tid, "title": f"task {tid}", "description": f"do {tid}"}
        for tid in TASK_IDS
    ]
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8",
    )


def _seed_parked_execution(
    *,
    project_dir: Optional[Path],
    plan_id: str = PLAN_ID,
) -> None:
    """Write the ``plan_execution`` row and the ``plan_tasks`` rows.

    The row is written through :class:`ExecutionRepository` and the
    per-task rows through :class:`PlanTaskRepository`, so the endpoint
    reads back exactly the shapes production writes.  ``exec_status`` is
    ``waiting_for_quota`` — the plan is parked, alive, and waiting for
    capacity — and the PID it still carries belongs to a process that
    has already exited: nothing about the row says "failed".
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = db_open(server._state_db_path())
    try:
        migrate(conn)
        row_fields: dict[str, Any] = {
            "exec_status": "waiting_for_quota",
            "exec_pid": _dead_pid(),
            "started_at": "2026-10-24T15:00:00",
        }
        if project_dir is not None:
            row_fields["project_dir"] = str(project_dir)
        ExecutionRepository(conn).update_fields(
            plan_id, create_if_missing=True, **row_fields,
        )

        task_repo = PlanTaskRepository(conn)
        for tid in TASK_IDS:
            task_repo.add_task(
                plan_id,
                {"id": tid, "title": f"task {tid}", "description": f"do {tid}"},
            )
        # Only the first task finished.  The second one is the one that
        # hit the provider wall; the third one is downstream of it.
        task_repo.update_task(
            plan_id, COMPLETED_TASK_ID, {"status": "completed"},
        )
    finally:
        conn.close()


def _client() -> TestClient:
    """A client that does NOT run the application lifespan.

    The lifespan is what restores ``_execution_state`` from the rows at
    boot.  Skipping it is deliberate: this contract is about the row
    being readable **on its own**, which is the state a server is in
    after it has been restarted with a plan it has never executed.
    """
    server._execution_state.clear()
    return TestClient(server.app)


def _seed_parked_run_with_a_dead_starter(
    *,
    project_dir: Optional[Path],
    plan_id: str = SURVIVOR_PLAN_ID,
) -> int:
    """Write the row and the task rows of a run whose starter has exited.

    The row says ``waiting_for_quota`` and carries the pid of the
    process that started the run — a process that has already exited,
    because the park ended it.  One task was interrupted by the park
    and still reads ``in_progress``; the two tasks downstream of it are
    still ``pending``.

    Returns the dead starter's pid, so the caller can leave this
    process holding the stale view of that run that the boot-time scan
    would have restored while the row still said ``running``.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    starter_pid = _dead_pid()
    conn = db_open(server._state_db_path())
    try:
        migrate(conn)
        row_fields: dict[str, Any] = {
            "exec_status": "waiting_for_quota",
            "exec_pid": starter_pid,
            "started_at": "2026-10-26T02:21:00",
        }
        if project_dir is not None:
            row_fields["project_dir"] = str(project_dir)
        ExecutionRepository(conn).update_fields(
            plan_id, create_if_missing=True, **row_fields,
        )

        task_repo = PlanTaskRepository(conn)
        for tid in TASK_IDS:
            task_repo.add_task(
                plan_id,
                {"id": tid, "title": f"task {tid}", "description": f"do {tid}"},
            )
        # The task that was interrupted by the park: it is still the
        # task the executor was working on when the wall hit.
        task_repo.update_task(
            plan_id, INTERRUPTED_TASK_ID, {"status": "in_progress"},
        )
    finally:
        conn.close()
    return starter_pid


def _leave_the_stale_starter_view(
    plan_id: str,
    starter_pid: int,
    project_dir: Optional[Path],
) -> None:
    """Leave this process holding the starter's stale view of the run.

    The boot-time recovery scan restores a row into the registry while
    the row still says ``running``.  The registry is never told about
    the park that happens afterwards, because the park is recorded by
    the process that owns the run — which is a different process from
    the one serving.  So the serving process keeps saying "running"
    with a pid that has since exited, and that stale view is exactly
    what the progress endpoint must not mistake for the truth.
    """
    server._execution_state[plan_id] = {
        "status": "running",
        "pid": starter_pid,
        "started_at": "2026-10-26T02:21:00",
        "ended_at": None,
        "stop_reason": None,
        "project_dir": str(project_dir) if project_dir else None,
        "process": None,
        "logs": deque(maxlen=2000),
    }


def _counts(body: dict) -> dict:
    return body.get("counts") or {}


# ---------------------------------------------------------------------------
# The fixture: a plan parked on quota
# ---------------------------------------------------------------------------


@pytest.fixture
def plans_dir(tmp_path: Path) -> Path:
    """The plans tree of this test — never the operator's."""
    path = tmp_path / "plans"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# 1. The parked plan reports its status and its counts
# ---------------------------------------------------------------------------


def test_parked_execution_progress_counts(plans_dir: Path) -> None:
    """A quota-parked plan answers the progress endpoint.

    The plan is parked: its row says ``waiting_for_quota`` and carries
    the PID of a process that has already exited.  The question the
    endpoint has to answer is the operator's — "what is still left to
    do?" — and the answer is in the row and in the task list.

    The counts are the criterion: the downstream task is still
    ``pending``.  It was not judged dead (``failed``) and it was not
    skipped — a quota wait is a pause, and a pause does not consume
    anybody's turn.
    """
    project_dir = plans_dir.parent / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    plan_dir = plans_dir / PLAN_ID
    _write_tasks_file(plan_dir)
    _seed_parked_execution(project_dir=project_dir)

    client = _client()
    resp = client.get(f"/api/execution/{PLAN_ID}/progress")

    assert resp.status_code == 200, (
        f"a parked plan must answer the progress endpoint; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert body["execution_status"] == "waiting_for_quota", (
        f"the endpoint reported {body['execution_status']!r}; the row "
        f"says the plan is parked, not running and not failed — a parked "
        f"plan that reads as 'running' or 'not_started' is the plan the "
        f"operator has to guess about"
    )

    counts = _counts(body)
    assert counts.get("total") == 3, f"unexpected counts: {counts!r}"
    assert counts.get("completed") == 1, f"unexpected counts: {counts!r}"
    assert counts.get("failed") == 0, (
        f"a quota wait must not fail anything: {counts!r}"
    )
    assert counts.get("in_progress") == 0, f"unexpected counts: {counts!r}"
    assert counts.get("pending") == 2, (
        f"the task that hit the wall and the task downstream of it must "
        f"still be pending: {counts!r}"
    )

    by_id = {t.get("id"): t for t in body.get("tasks") or []}
    assert by_id.get("VP-003", {}).get("status") == "pending", (
        f"the downstream task must stay pending; got "
        f"{by_id.get('VP-003')!r}"
    )
    assert by_id.get("VP-002", {}).get("status") == "pending", (
        f"the parked task must stay pending, not failed and not skipped; "
        f"got {by_id.get('VP-002')!r}"
    )


# ---------------------------------------------------------------------------
# 2. A parked plan with no project dir still answers
# ---------------------------------------------------------------------------


def test_progress_without_project_dir_still_reports_counts(
    plans_dir: Path,
) -> None:
    """The row is the evidence — the project dir is not.

    A parked plan does not have to carry a project directory: the
    executor may have parked before it got one, and a row written by
    another component may simply not have the column filled in.  The
    endpoint used to read the missing project dir as "this plan was
    never configured for execution" and answered 404, which told the
    operator to call ``/start`` on a plan that was already running and
    waiting for capacity.

    The row says the plan WAS configured.  So the endpoint must answer
    with the row's status and the counts.
    """
    plan_dir = plans_dir / PLAN_ID
    _write_tasks_file(plan_dir)
    _seed_parked_execution(project_dir=None)

    client = _client()
    resp = client.get(f"/api/execution/{PLAN_ID}/progress")

    assert resp.status_code == 200, (
        f"a plan with a readable execution row must answer the progress "
        f"endpoint even without a project dir; got {resp.status_code}: "
        f"{resp.text!r}"
    )
    body = resp.json()

    assert body["execution_status"] == "waiting_for_quota", (
        f"the endpoint reported {body['execution_status']!r}; the row "
        f"is the only place the park is recorded, so it has to reach "
        f"the answer"
    )

    counts = _counts(body)
    assert counts.get("total") == 3, f"unexpected counts: {counts!r}"
    assert counts.get("completed") == 1, f"unexpected counts: {counts!r}"
    assert counts.get("failed") == 0, (
        f"a quota wait must not fail anything: {counts!r}"
    )
    assert counts.get("in_progress") == 0, f"unexpected counts: {counts!r}"
    assert counts.get("pending") == 2, (
        f"the parked task and the one downstream of it must still be "
        f"pending: {counts!r}"
    )

    assert body.get("project_dir") in ("", None), (
        f"there is no project dir to report; got {body.get('project_dir')!r}"
    )


# ---------------------------------------------------------------------------
# 3. The negative control: no execution record, still 404
# ---------------------------------------------------------------------------


def test_no_execution_row_still_404(plans_dir: Path) -> None:
    """A plan with no execution record at all is still "not configured".

    This is the other half of the rule, and the half that keeps the
    first half honest: the 404 is not "the plan has no project
    directory", it is "there is no execution record for this plan".
    A plan that was never started has no row, so it keeps the original
    message — an operator who reads that message can act on it.
    """
    plan_dir = plans_dir / PLAN_ID
    _write_tasks_file(plan_dir)

    client = _client()
    resp = client.get(f"/api/execution/{PLAN_ID}/progress")

    assert resp.status_code == 404, (
        f"a plan with no execution record must still answer 404; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    assert "not configured" in resp.text, (
        f"the 404 must keep its original wording so an operator knows "
        f"what to do; got {resp.text!r}"
    )


# ---------------------------------------------------------------------------
# 4. The service outlives the process that started the run
# ---------------------------------------------------------------------------


def test_progress_counts_stay_answerable_after_the_round_service_outlives_its_starter(
    plans_dir: Path,
) -> None:
    """The counts stay answerable after the starter of the run is gone.

    The round's service outlived the process that started the run: the
    executor parked on quota and exited, and the service that answers
    for the plan is still up.  The operator still has to be told what
    is left to do — the interrupted task is still the task that was in
    flight when the wall hit, the two tasks downstream of it are still
    waiting, and nothing has failed.

    A quota wait is a pause, and a pause is not a crash.  The run's
    process being gone says the run is *not in flight*; it says nothing
    about the work having failed.  So the answer is 200 with the counts
    the row and the task rows carry, and it says WHICH instance
    produced it — so a reader can tell this answer apart from another
    instance's answer about the same plan.

    The serving process's own view of the run is stale: it still says
    ``running``, because the park was recorded by the process that
    owned the run, not by the one serving.  The row is the truth, and
    the row's truth is what the answer must be made of.
    """
    project_dir = plans_dir.parent / "project-survivor"
    project_dir.mkdir(parents=True, exist_ok=True)
    plan_dir = plans_dir / SURVIVOR_PLAN_ID
    _write_tasks_file(plan_dir)

    starter_pid = _seed_parked_run_with_a_dead_starter(
        project_dir=project_dir,
    )
    _leave_the_stale_starter_view(
        SURVIVOR_PLAN_ID, starter_pid, project_dir,
    )

    # The client must NOT run the lifespan: the boot-time scan would
    # re-read the row and overwrite the stale view with the row's own
    # verdict, and the case this test is about is the one where the
    # serving process never caught up.
    client = TestClient(server.app)
    resp = client.get(f"/api/execution/{SURVIVOR_PLAN_ID}/progress")

    assert resp.status_code == 200, (
        f"the counts must stay answerable after the starter is gone; got "
        f"{resp.status_code}: {resp.text!r}"
    )
    body = resp.json()

    assert body["execution_status"] == "waiting_for_quota", (
        f"the endpoint reported {body['execution_status']!r}; the run "
        f"parked on capacity and its process then exited — a pause that "
        f"ends with the process gone is still a pause, not a crash"
    )

    counts = _counts(body)
    assert counts.get("total") == 3, f"unexpected counts: {counts!r}"
    assert counts.get("completed") == 0, f"unexpected counts: {counts!r}"
    assert counts.get("failed") == 0, (
        f"a quota wait must not fail anything: {counts!r}"
    )
    assert counts.get("in_progress") == 1, (
        f"the task that was interrupted by the park must still read as "
        f"in flight — it was interrupted, never judged: {counts!r}"
    )
    assert counts.get("pending") == 2, (
        f"the two tasks downstream of the interrupted one must still be "
        f"pending: {counts!r}"
    )

    by_id = {t.get("id"): t for t in body.get("tasks") or []}
    assert by_id.get(INTERRUPTED_TASK_ID, {}).get("status") == "in_progress", (
        f"the interrupted task must still read in_progress; got "
        f"{by_id.get(INTERRUPTED_TASK_ID)!r}"
    )
    for downstream in ("VP-002", "VP-003"):
        assert by_id.get(downstream, {}).get("status") == "pending", (
            f"{downstream} is downstream of the interrupted task and must "
            f"still be pending; got {by_id.get(downstream)!r}"
        )

    # The run is not in flight: its executor process is gone and the
    # plan is parked, not running.  The row keeps the pid the executor
    # will be resumed under, so the pid alone would say "alive".
    assert body.get("execution_in_flight") is False, (
        f"a parked run whose process is gone is not in flight; got "
        f"{body.get('execution_in_flight')!r}"
    )

    # The answer says which instance produced it — otherwise a reader
    # cannot tell this answer from another instance's answer about the
    # same plan.
    serving = body.get("serving_instance")
    assert isinstance(serving, dict), (
        f"the answer must name the instance that produced it; got "
        f"{serving!r}"
    )
    assert serving.get("pid") == os.getpid(), (
        f"the serving instance is this process; got {serving.get('pid')!r}"
    )
    assert serving.get("boot_id"), (
        f"the serving instance must carry a boot id; got "
        f"{serving.get('boot_id')!r}"
    )
    assert serving.get("started_at"), (
        f"the serving instance must carry its start time; got "
        f"{serving.get('started_at')!r}"
    )
    db_path = server._state_db_path()
    assert serving.get("state_db_path") == str(db_path), (
        f"the serving instance must name the database it read; got "
        f"{serving.get('state_db_path')!r}, expected {str(db_path)!r}"
    )
    assert serving.get("state_db_inode") == db_path.stat().st_ino, (
        f"the serving instance must name the inode of the database it "
        f"read; got {serving.get('state_db_inode')!r}"
    )

    # And the poll itself must not have judged the pause a crash: the
    # row still says the plan is parked, and the interrupted task is
    # still the one that was in flight.
    from state_machine.db.connection import open as db_open
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = db_open(server._state_db_path())
    try:
        row = ExecutionRepository(conn).summary(SURVIVOR_PLAN_ID)
        runtime = PlanTaskRepository(conn).load_all(SURVIVOR_PLAN_ID)
    finally:
        conn.close()

    assert row.get("exec_status") == "waiting_for_quota", (
        f"a progress poll must not rewrite a parked run as a crash; the "
        f"row now says {row.get('exec_status')!r}"
    )
    assert (runtime.get(INTERRUPTED_TASK_ID) or {}).get("status") == (
        "in_progress"
    ), (
        f"a progress poll must not reap the interrupted task of a parked "
        f"run; the row now says "
        f"{(runtime.get(INTERRUPTED_TASK_ID) or {}).get('status')!r}"
    )

