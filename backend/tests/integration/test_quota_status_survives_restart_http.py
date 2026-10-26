"""HTTP contract: a quota-parked execution is still parked after a restart.

The criterion this file pins
----------------------------
The import acceptance criterion for the capacity-wait work reads:

    *the plan must not be judged dead — its status must be readable from
    the interface*

It was checked once, by hand, with a probe against the shared port::

    GET /api/execution/20261024-vp-quota-parked/status   ->  connection refused

The probe said nothing about the criterion.  The backend had already
been recycled before the request was made, so the answer was about the
*service's* lifetime, not about the *plan's* status — the criterion was
neither met nor refuted.  A one-shot probe against a shared port is not
evidence.

This file turns the criterion into a repeatable HTTP contract test:

  * ``test_waiting_execution_survives_startup_recovery``
    The whole application boots — the real FastAPI lifespan, which is
    what calls ``routes.execution._recover_execution_states`` — and the
    parked plan is then read over HTTP.  The answer must be
    ``waiting_for_quota``, not ``failed``.

  * ``test_lazy_watchdog_does_not_flip_a_waiting_execution``
    The runtime watchdog (``verification_loop._lazy_check_execution``)
    probes only executions that claim to be ``running``.  A parked
    execution carries a PID whose process is long gone; the watchdog
    must not read that as a dead run.

  * ``test_vp_003_parked_execution_survives_restart_as_waiting_for_quota``
    The read path itself.  The two tests above read the answer out of
    the in-memory map, which the boot-time recovery fills in — so they
    say nothing about a process that has *no* in-memory record of the
    plan: a fresh boot whose recovery scan has not run yet, or a second
    process answering for a plan the first one is running.  The durable
    row is what survived the restart, so the durable row is what the
    answer must come from.

  * ``test_parked_row_without_a_reason_still_says_why`` and
    ``test_parking_persists_the_reason_on_the_row``
    The reason, not only the verdict.  A quota wait has exactly one
    reason, so a parked row says *why* it stopped — and both read
    paths (the boot-time recovery and the row fallback) apply the same
    rule, because an answer that differs between them makes the
    verdict depend on which process served the request.

One rule, two homes: **only a ``running`` execution is probed for
liveness**.  The boot-time recovery and the runtime watchdog both carry
that rule, and both must leave a quota-parked plan parked.

The fixtures are hermetic: the plans tree and the state database live
under the test's own ``tmp_path`` — the autouse ``isolated_plans_dir``
fixture points ``server.PLANS_DIR`` / ``server._state_db_path`` and the
``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH`` env vars at it — so the real
database and the operator's ``plans/`` tree are never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest
from fastapi.testclient import TestClient

import server
from routes import execution as routes_execution


#: The plan id the failed probe used.  Reusing it keeps the criterion
#: and its evidence about the same plan.
PLAN_ID = "20261024-vp-quota-parked"


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


def _seed_execution_row(plan_id: str, **fields: Any) -> None:
    """Write the ``plan_execution`` row through the real repository.

    Written through ``ExecutionRepository`` so the row has the shape
    ``_recover_execution_states`` reads back via ``summary()`` — the
    recovery scan must not be handed a row the production code would
    never write.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = db_open(server._state_db_path())
    try:
        migrate(conn)
        ExecutionRepository(conn).update_fields(
            plan_id,
            create_if_missing=True,
            **fields,
        )
    finally:
        conn.close()


def _exec_status(plan_id: str) -> str:
    """The ``exec_status`` the state machine holds for ``plan_id``."""
    from state_machine.db.connection import open as db_open
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = db_open(server._state_db_path())
    try:
        return ExecutionRepository(conn).summary(plan_id)["exec_status"]
    finally:
        conn.close()


def _execution_row(plan_id: str) -> dict:
    """The whole ``plan_execution`` row the state machine holds.

    Read through the repository so the assertions see the same shape
    production reads back — including the columns a read must not
    change (``exec_pid``, ``stop_reason``).
    """
    from state_machine.db.connection import open as db_open
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = db_open(server._state_db_path())
    try:
        return ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()


def _spy_on_mark_failed_dead(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Record every plan the code under test tries to mark dead.

    The spy delegates to the real function, so a plan that IS wrongly
    judged dead still ends up ``failed`` — the record is not what makes
    the failure visible, it is what makes it *attributable*.
    """
    marked: List[str] = []
    real_mark = server._mark_failed_dead

    def _spy(plan_id: str) -> None:
        marked.append(plan_id)
        return real_mark(plan_id)

    monkeypatch.setattr(server, "_mark_failed_dead", _spy)
    return marked


# ---------------------------------------------------------------------------
# The fixture: a plan parked on quota, with a dead PID in its row
# ---------------------------------------------------------------------------


@pytest.fixture
def parked_execution(tmp_path: Path) -> SimpleNamespace:
    """A quota-parked plan whose row still carries an exited process.

    This is the state the PRD criterion is about: the executor parked
    the run because the provider had no quota, the row says
    ``waiting_for_quota`` — verdict *and* reason, which is what
    production persists when it parks a run — and the PID it still
    carries belongs to a process that has already exited.  Nothing
    about it says "failed".
    """
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # The database the seed lands in must be this test's own — never
    # the operator's live state.db.  The autouse ``isolated_plans_dir``
    # fixture is what redirects it; this assertion is the proof that
    # the redirection actually happened before anything was written.
    db_path = server._state_db_path()
    assert str(db_path).startswith(str(tmp_path)), (
        f"the state database resolves to {db_path}, which is outside the "
        f"test's tmp_path — the seed would land in a real database"
    )

    dead_pid = _dead_pid()
    _seed_execution_row(
        PLAN_ID,
        exec_status="waiting_for_quota",
        stop_reason="waiting_for_quota",
        exec_pid=dead_pid,
        project_dir=str(project_dir),
        started_at="2026-10-24T15:00:00",
    )

    return SimpleNamespace(
        plan_id=PLAN_ID,
        plans_dir=plans_dir,
        plan_dir=plan_dir,
        project_dir=project_dir,
        dead_pid=dead_pid,
    )


# ---------------------------------------------------------------------------
# 1. The status is readable from the interface after a restart
# ---------------------------------------------------------------------------


def test_waiting_execution_survives_startup_recovery(
    parked_execution: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked execution comes back from the boot as a parked execution.

    The whole application boots here — the real FastAPI lifespan — and
    the lifespan is what calls
    ``routes.execution._recover_execution_states``.  That scan walks the
    plans tree and probes the PID of every row that claims to be
    ``running``.  A quota-parked row is not running, so its PID is not
    probed and its status is not touched.

    The answer the interface gives must therefore be
    ``waiting_for_quota`` — the whole criterion in one field.
    """
    marked = _spy_on_mark_failed_dead(monkeypatch)

    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{PLAN_ID}/status")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "waiting_for_quota", (
            f"the boot turned a quota-parked execution into "
            f"{body['status']!r}; the criterion says the plan must not "
            f"be judged dead — {body!r}"
        )
        assert body["ended_at"] is None, (
            f"a parked execution has not ended: {body!r}"
        )

    assert marked == [], (
        f"the startup recovery tried to mark {marked!r} dead; only a "
        f"running execution may be probed for liveness"
    )
    assert _exec_status(PLAN_ID) == "waiting_for_quota", (
        "the startup recovery rewrote the parked row's status"
    )


# ---------------------------------------------------------------------------
# 2. The runtime watchdog leaves it alone too
# ---------------------------------------------------------------------------


def test_lazy_watchdog_does_not_flip_a_waiting_execution(
    parked_execution: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog probes running executions only.

    The boot-time recovery is one half of the rule; this is the other.
    The runtime watchdog (``verification_loop._lazy_check_execution``)
    runs against the in-memory state and asks the same question the
    recovery asks — "is the process still there?" — but it must ask it
    of ``running`` executions only.

    A parked execution holds a PID whose process is gone.  Probing it
    would produce a ``ProcessLookupError``, and the handler for that
    is the one that marks a run dead.  So the watchdog must not get
    that far: the parked execution must come out of the watchdog
    exactly as it went in.
    """
    import verification_loop

    # The state the startup recovery restored: parked, with the dead
    # PID still attached.  Restored through the real recovery function,
    # so the watchdog is handed the same state it meets in production.
    server._execution_state.clear()
    server._recover_execution_states(parked_execution.plans_dir)

    restored = server._execution_state.get(PLAN_ID)
    assert restored is not None, (
        "the startup recovery did not restore the parked execution at all"
    )
    assert restored["status"] == "waiting_for_quota", (
        f"the startup recovery restored {restored['status']!r} instead "
        f"of the row's own status"
    )
    assert restored["pid"] == parked_execution.dead_pid, (
        "the restored state lost the PID — the watchdog would have "
        "nothing to probe and the test would prove nothing"
    )

    marked = _spy_on_mark_failed_dead(monkeypatch)

    verification_loop._lazy_check_execution(PLAN_ID)

    # And the interface still answers with the parked status — the
    # criterion is about what a reader sees, not only about the row.
    # The status endpoint runs the watchdog itself, so this read is a
    # second, independent trip through the same rule.
    client = TestClient(server.app)
    resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert marked == [], (
        f"the watchdog tried to mark {marked!r} dead; a parked execution "
        f"is not a dead execution, and only a running one may be probed"
    )
    assert server._execution_state[PLAN_ID]["status"] == "waiting_for_quota", (
        f"the watchdog flipped the parked execution to "
        f"{server._execution_state[PLAN_ID]['status']!r}"
    )
    assert server._execution_state[PLAN_ID]["ended_at"] is None, (
        "the watchdog ended the parked execution"
    )
    assert _exec_status(PLAN_ID) == "waiting_for_quota", (
        "the watchdog rewrote the parked row's status"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "waiting_for_quota", resp.text


# ---------------------------------------------------------------------------
# 3. The read path answers from the durable row
# ---------------------------------------------------------------------------


def test_vp_003_parked_execution_survives_restart_as_waiting_for_quota(
    parked_execution: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked execution reads as parked from the row, after a restart.

    The two tests above read the answer out of the in-memory map, which
    the boot-time recovery fills in.  They therefore say nothing about
    the process that has *no* in-memory record of the plan — and that
    process is not a corner case, it is the ordinary one:

      * a fresh boot whose recovery scan has not run yet, and
      * a second process answering for a plan the first one is running.

    Both used to answer "not started": the status endpoint read the
    in-memory map only, and invented an empty payload when the map held
    nothing.  That is the contract this test pins down — a parked plan
    that survives the restart as ``not_started`` reads as "never ran",
    and the operator is told the run never happened.

    The answer must come from the durable row, which is what survived:

      * ``status`` is ``waiting_for_quota`` — not ``failed`` (a crash
        reads as failed) and not ``stopped`` (an operator's
        cancellation reads as stopped);
      * ``stop_reason`` is ``waiting_for_quota`` — the reason the run
        stopped, which is the whole difference between "we are waiting
        for capacity" and "this died";
      * the body carries no Python traceback — a read that crashed must
        not answer with a stack trace.

    And the read judges nothing: a parked run is not a dead run, so the
    fallback must not probe the PID that the row still carries.  Liveness
    belongs to ``_recover_execution_states`` and ``_lazy_check_execution``
    — the two places that are allowed to ask "is the process still there?".

    The client is built with ``raise_server_exceptions=False`` so that a
    crash inside the route shows up as a response body to assert on
    rather than as an exception that hides what the answer was.
    """
    # Nothing in memory: this is the state of every process that did
    # not start the run.
    server._execution_state.clear()

    marked = _spy_on_mark_failed_dead(monkeypatch)

    # The client is deliberately NOT entered as a context manager: the
    # lifespan is what fills the in-memory map back in, and this test is
    # about the process that has no such record to fill.
    client = TestClient(server.app, raise_server_exceptions=False)
    resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    assert "Traceback" not in resp.text, (
        f"the answer carries a Python traceback: {resp.text}"
    )
    body = resp.json()
    assert body["status"] == "waiting_for_quota", (
        f"the parked plan reads as {body['status']!r} once the in-memory "
        f"record is gone; it must still be parked — {body!r}"
    )
    assert body["stop_reason"] == "waiting_for_quota", (
        f"the answer does not say why the run stopped: {body!r}"
    )
    assert body["ended_at"] is None, (
        f"a parked execution has not ended: {body!r}"
    )
    assert body["pid"] == parked_execution.dead_pid, (
        f"the answer lost the pid the row carries: {body!r}"
    )

    # The read path judges nothing: nothing may mark the plan dead.
    assert marked == [], (
        f"the read path tried to mark {marked!r} dead; only a running "
        f"execution may be probed for liveness"
    )

    # And the row is untouched — a read does not rewrite the verdict.
    row = _execution_row(PLAN_ID)
    assert row is not None, "the parked row vanished"
    assert row["exec_status"] == "waiting_for_quota", (
        f"the row now says {row['exec_status']!r}: {row!r}"
    )
    assert row["stop_reason"] == "waiting_for_quota", (
        f"the row lost the reason it was parked for: {row!r}"
    )
    assert row["exec_pid"] == parked_execution.dead_pid, (
        f"the row's pid was rewritten: {row!r}"
    )


#: The plan's gate command for ``repair-r1-26`` names the contract of
#: the test above under a different name:
#: ``test_status_reads_the_persisted_row_when_memory_is_empty``.  The
#: section title above ("the read path answers from the durable row")
#: and that name say the same thing -- the answer must come from the
#: persisted row when the in-memory record is empty.  Both names point
#: at the same test, so both gates run the same assertions.
test_status_reads_the_persisted_row_when_memory_is_empty = (
    test_vp_003_parked_execution_survives_restart_as_waiting_for_quota
)


# ---------------------------------------------------------------------------
# 4. The rule is the same on both read paths
# ---------------------------------------------------------------------------


def test_parked_row_without_a_reason_still_says_why(
    parked_execution: SimpleNamespace,
) -> None:
    """A parked row that carries no reason of its own still says why.

    A row parked before the reason was persisted alongside the status
    carries the verdict and nothing else.  A quota wait has exactly one
    reason — the capacity wall — so the answer must still name it: an
    answer that says "parked" but not "why" leaves the operator
    guessing.

    Both read paths apply the same rule — the boot-time recovery and
    the status endpoint's row fallback — because an answer that differs
    between them makes the verdict depend on which process served the
    request, which is the defect this file is about.
    """
    # The legacy shape: the verdict, no reason.
    _seed_execution_row(
        PLAN_ID,
        exec_status="waiting_for_quota",
        stop_reason=None,
        exec_pid=parked_execution.dead_pid,
        project_dir=str(parked_execution.project_dir),
        started_at="2026-10-24T15:00:00",
    )

    server._execution_state.clear()
    client = TestClient(server.app, raise_server_exceptions=False)
    resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "waiting_for_quota", body
    assert body["stop_reason"] == "waiting_for_quota", (
        f"the answer does not say why the run stopped: {body!r}"
    )

    # And the boot-time recovery applies the same rule.
    server._execution_state.clear()
    server._recover_execution_states(parked_execution.plans_dir)

    restored = server._execution_state.get(PLAN_ID)
    assert restored is not None, (
        "the startup recovery did not restore the parked execution at all"
    )
    assert restored["status"] == "waiting_for_quota", restored
    assert restored["stop_reason"] == "waiting_for_quota", (
        f"the restored state lost the reason the run stopped for: "
        f"{restored!r}"
    )


# ---------------------------------------------------------------------------
# 5. The write half: parking persists the reason, not only the verdict
# ---------------------------------------------------------------------------


def test_parking_persists_the_reason_on_the_row(
    parked_execution: SimpleNamespace,
) -> None:
    """A parked run leaves its reason on the row.

    The row is the record that outlives the process, so it has to carry
    the whole verdict: a row that says ``waiting_for_quota`` and no
    reason leaves every reader to re-derive what the status already
    says — and the readers are not all in this process.

    This is the write half of the contract the read half
    (``test_vp_003_parked_execution_survives_restart_as_waiting_for_quota``)
    depends on.
    """
    # The executor's own marker: the run parked because every provider
    # had no capacity left.
    marker = {
        "providers": ["vendor-a"],
        "needs_manual_topup": True,
        "waited_sec": 42.5,
        "at": "2026-10-24T15:00:00",
    }
    tasks_file = parked_execution.plan_dir / "tasks.json"
    tasks_file.write_text(
        json.dumps(
            {
                "tasks": [{"id": "1", "title": "a task"}],
                "stop_reason": "waiting_for_quota",
                "reason_detail": json.dumps(marker),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = {"status": "running", "stop_reason": None, "ended_at": None}
    assert routes_execution._enter_quota_wait(
        PLAN_ID, parked_execution.plan_dir, state, tasks_file,
    ) is True, "the run did not park — the quota marker was not read"

    row = _execution_row(PLAN_ID)
    assert row["exec_status"] == "waiting_for_quota", (
        f"the row does not carry the parked verdict: {row!r}"
    )
    assert row["stop_reason"] == "waiting_for_quota", (
        f"the row does not carry the reason the run stopped for: {row!r}"
    )
