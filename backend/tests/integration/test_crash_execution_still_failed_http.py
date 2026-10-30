"""HTTP contract: a crashed execution is still failed after a restart.

The criterion this file pins
----------------------------
The import acceptance criterion for the capacity-wait work reads:

    *only a capacity fact may enter the wait — a crash is not
    "waiting for quota"*

It was checked once, by hand, with a probe against the shared port::

    GET /api/execution/20261024-vp-quota-crash/status   ->  connection refused

The probe said nothing about the criterion.  The backend had already been
recycled before the request was made, so the answer was about the
*service's* lifetime, not about the *plan's* verdict — the criterion was
neither met nor refuted.  A one-shot probe against a shared port is not
evidence.

This file turns the criterion into a repeatable HTTP contract test:

  * ``test_crashed_running_execution_is_marked_failed``
    The whole application boots — the real FastAPI lifespan, which is
    what calls ``routes.execution._recover_execution_states`` — and the
    crashed plan is then read over HTTP.  The answer must be ``failed``
    with ``stop_reason='process_died_unexpectedly'``.  A crash is not a
    quota wait: nothing about the run may read as ``waiting_for_quota``.

  * ``test_crash_verdict_survives_a_restart``
    The crash verdict has to outlive the process that noticed it.  The
    runtime watchdog (``verification_loop._lazy_check_execution``) is the
    other home of the same rule; after it has judged the run dead, a
    restart must read back the *same* verdict from the row — not
    ``running``, not ``not_started``, and not a lost ``stop_reason``.

  * ``test_vp_009_crashed_execution_without_marker_stays_failed``
    The negative control of the capacity-wait criterion, as a contract
    test: a process that really died and carries no wait marker must
    still be judged ``failed`` — it must never be washed into "waiting
    for quota".  It also pins the half that made the criterion fail in
    the first place: the verdict has to be *written* when it is reached
    and *replayable* afterwards, so a reader that holds nothing but the
    row reads back the same verdict with the same end time.

One rule, two homes: **only a ``running`` execution is probed for
liveness, and a probe that finds the process gone is a crash.**  The
boot-time recovery and the runtime watchdog both carry that rule, and
both must leave a crashed plan failed.

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
from collections import deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import server


#: The plan id the failed probe used.  Reusing it keeps the criterion
#: and its evidence about the same plan.
PLAN_ID = "20261024-vp-quota-crash"

#: A plan that has no ``plan_execution`` row at all — nobody ever ran
#: it.  Used by the VP-009 negative control.
VP009_NO_ROW_PLAN_ID = "20261024-vp-quota-never-ran"

#: A plan parked on capacity.  Its row says ``waiting_for_quota``, so it
#: must never enter the crash path.
VP009_PARKED_PLAN_ID = "20261024-vp-quota-parked"


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


def _instant(value: str) -> datetime:
    """The instant a stored start-time stamp names.

    A naive stamp was written by ``datetime.now()`` — local time — so it
    is read as local time here, exactly as the product reads it
    (``routes.execution._normalize_started_at``).  Reading it as UTC
    would move the instant by the writer's offset and answer a
    different question than the one being asked.

    Two stamps that name the same instant are the same moment; two
    stamps that spell it differently are not two moments.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        # ``fromisoformat`` only learned to read a trailing ``Z`` in
        # 3.11; spelling the offset out keeps this identical on every
        # interpreter the suite runs on.
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _execution_row(plan_id: str) -> dict:
    """The ``plan_execution`` row the state machine holds for ``plan_id``."""
    from state_machine.db.connection import open as db_open
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = db_open(server._state_db_path())
    try:
        return ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()


def _migrate_state_db() -> None:
    """Make sure the state database carries the schema.

    A plan that never ran has no row — and a database that never held
    a row has no table either.  Reading "there is no row" therefore
    needs the schema to exist, otherwise the read fails on the table
    instead of answering "no row".
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate

    conn = db_open(server._state_db_path())
    try:
        migrate(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The fixture: a plan that says it is running, with a dead PID in its row
# ---------------------------------------------------------------------------


@pytest.fixture
def crashed_execution(tmp_path: Path) -> SimpleNamespace:
    """A plan whose row claims ``running`` while its process is gone.

    This is the state the PRD criterion is about: the executor died
    without ever reaching a terminal state, the row still says
    ``running``, and the PID it still carries belongs to a process that
    has already exited.  Nothing about it says "waiting for quota" — the
    run crashed.
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
        exec_status="running",
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


def _running_in_memory_state(
    plan_id: str, pid: int, project_dir: Path
) -> None:
    """Put ``plan_id`` into the in-memory state a live server would hold.

    The runtime watchdog reads the in-memory dict, not the row, so the
    watchdog half of the rule needs the state a running server would
    have built.  The shape is the one
    ``routes.execution._recover_execution_states`` restores.
    """
    server._execution_state[plan_id] = {
        "status": "running",
        "started_at": "2026-10-24T15:00:00",
        "ended_at": None,
        "pid": pid,
        "stop_reason": None,
        "project_dir": str(project_dir),
        "logs": deque(maxlen=2000),
        "process": None,
    }


# ---------------------------------------------------------------------------
# 1. A crashed execution reads as failed — never as a quota wait
# ---------------------------------------------------------------------------


def test_crashed_running_execution_is_marked_failed(
    crashed_execution: SimpleNamespace,
) -> None:
    """A crashed execution is ``failed``, and says why.

    The whole application boots here — the real FastAPI lifespan — and
    the lifespan is what calls
    ``routes.execution._recover_execution_states``.  That scan walks the
    plans tree and probes the PID of every row that claims to be
    ``running``.  This one claims to be running and its process is gone,
    so the probe must find it dead and the run must be marked failed.

    The verdict has to be legible from the interface, because that is
    the only place an operator can see it:

      * ``status`` is ``failed`` — a crash is not a quota wait, and the
        answer must not read as ``waiting_for_quota``;
      * ``stop_reason`` is ``process_died_unexpectedly`` — the reason the
        run ended, which is the whole difference between "we are waiting
        for capacity" and "this died";
      * no live pid is reported.  A pid belongs to a process that is
        gone; every "is this execution alive?" reader believes a pid
        means alive.
    """
    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed", (
        f"the crash was read as {body['status']!r}; a crashed run is "
        f"failed — and it is certainly not a quota wait — {body!r}"
    )
    assert body["stop_reason"] == "process_died_unexpectedly", (
        f"the interface does not say why the run ended: {body!r}"
    )
    assert body["ended_at"] is not None, (
        f"a failed execution has ended, so it must carry an end time: "
        f"{body!r}"
    )
    assert body["pid"] is None, (
        f"the interface still reports pid {body['pid']!r} for a run whose "
        f"process is gone — a pid reads as alive to every consumer"
    )

    # And the row says the same thing, so the verdict outlives this
    # process.
    row = _execution_row(PLAN_ID)
    assert row["exec_status"] == "failed", (
        f"the row still says {row['exec_status']!r}: {row!r}"
    )
    assert row["stop_reason"] == "process_died_unexpectedly", (
        f"the row does not record why the run ended: {row!r}"
    )
    assert row["exec_pid"] is None, (
        f"the row still carries pid {row['exec_pid']!r} for a run whose "
        f"process is gone: {row!r}"
    )


# ---------------------------------------------------------------------------
# 2. The verdict survives a restart
# ---------------------------------------------------------------------------


def test_crash_verdict_survives_a_restart(
    crashed_execution: SimpleNamespace,
) -> None:
    """The crash verdict is durable, and the watchdog agrees with the boot.

    Two halves, one contract.

    The runtime watchdog (``verification_loop._lazy_check_execution``)
    is the other home of the liveness rule: it probes the in-memory
    state and, on ``ProcessLookupError``, marks the run dead.  It must
    reach the same verdict the boot-time recovery reaches — ``failed`` /
    ``process_died_unexpectedly`` — and never a quota wait.

    Then the durability half: after the verdict is written, the process
    that noticed the crash is gone.  The next boot finds only the row,
    so the row has to carry the whole verdict.  If it carries only the
    status, the restart reads back a failed run with no reason — and if
    it carries nothing at all, the restart reads back ``running`` (the
    row still claims it) and re-probes a dead pid, or ``not_started``.
    Either way the operator is told a crash was something else.

    So after the restart the interface must still answer ``failed`` /
    ``process_died_unexpectedly``, and the row must still carry that
    verdict with no pid left on it.
    """
    import verification_loop

    # --- the runtime half of the rule -----------------------------------
    _running_in_memory_state(PLAN_ID, crashed_execution.dead_pid,
                              crashed_execution.project_dir)

    verification_loop._lazy_check_execution(PLAN_ID)

    state = server._execution_state.get(PLAN_ID)
    assert state is not None, "the watchdog dropped the execution state"
    assert state["status"] == "failed", (
        f"the watchdog read the dead process as {state['status']!r}; a "
        f"crashed run is failed — and it is certainly not a quota wait"
    )
    assert state["stop_reason"] == "process_died_unexpectedly", (
        f"the watchdog did not record why the run ended: {state!r}"
    )

    row = _execution_row(PLAN_ID)
    assert row["exec_status"] == "failed", (
        f"the watchdog left the row saying {row['exec_status']!r}: {row!r}"
    )
    assert row["stop_reason"] == "process_died_unexpectedly", (
        f"the watchdog did not write the verdict to the row: {row!r}"
    )
    assert row["exec_pid"] is None, (
        f"the row still carries pid {row['exec_pid']!r} for a run whose "
        f"process is gone: {row!r}"
    )

    # --- the restart half ------------------------------------------------
    # A fresh process has no in-memory state at all: the row is the only
    # thing that survived.
    server._execution_state.clear()

    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed", (
        f"after the restart the crash reads as {body['status']!r}; the "
        f"verdict did not survive — {body!r}"
    )
    assert body["status"] != "waiting_for_quota", (
        f"a crashed run was read as a quota wait: {body!r}"
    )
    assert body["stop_reason"] == "process_died_unexpectedly", (
        f"after the restart the interface does not say why the run ended: "
        f"{body!r}"
    )
    assert body["pid"] is None, (
        f"after the restart the interface reports pid {body['pid']!r} for "
        f"a run whose process is gone — a pid reads as alive to every "
        f"consumer"
    )


# ---------------------------------------------------------------------------
# 3. VP-009 — the crash verdict is written when it is reached, and replayed
# ---------------------------------------------------------------------------


def test_vp_009_crashed_execution_without_marker_stays_failed(
    crashed_execution: SimpleNamespace,
) -> None:
    """A dead process with no wait marker is ``failed`` — and stays failed.

    This is the negative control of the capacity-wait criterion:

        *a process that really died and carries no wait marker must still
        be judged ``failed`` — it must never be washed into "waiting for
        quota"*.

    The earlier rounds fixed *which* inputs the recovery scan and the
    watchdog judge from.  This round fixes **when the verdict is written
    and whether it can be replayed**: the moment the process death is
    recognised, the whole verdict goes to the ``plan_execution`` row —
    status, reason and the (now meaningless) pid, in one write through
    ``ExecutionRepository``.  The table has no end-time column of its
    own, so the row's own stamp of that write is the end time: the
    write *is* the verdict.  From that moment on the row is the only
    truth: every later read — after a restart, from another process,
    from a poller that never saw the run — replays it instead of
    re-deriving it, and a re-derivation is exactly what produced
    ``status=not_started, stop_reason=null`` while the run was dying.

    Four halves, one contract:

    1. **The write happens when the verdict is reached.**  The row says
       ``failed`` / ``process_died_unexpectedly`` with the pid cleared
       and the write stamped — not a row that still says ``running``
       while the process is gone.
    2. **The verdict replays.**  A reader that holds no in-memory state
       (another process, or the next boot) reads the same verdict off
       the row, with the *same* end time — one verdict, one timestamp,
       no second derivation.
    3. **No row means "not started".**  A plan nobody ever ran must not
       be answered with a fabricated failure.
    4. **A parked run never enters the crash path.**  A row that says
       ``waiting_for_quota`` is left alone: only a ``running`` row may
       be re-judged, so a quota pause can never be reported as a crash.
    """
    plan_id = crashed_execution.plan_id

    # The negative control's premise, written down: the plan carries NO
    # wait marker.  ``tasks.json`` says nothing about waiting, so there
    # is no capacity fact that could justify a quota disposition.
    (crashed_execution.plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "T1", "status": "in_progress"}]}),
        encoding="utf-8",
    )

    # --- 1. the verdict is written when it is reached --------------------
    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed", (
        f"the crashed run reads as {body['status']!r}; a process that died "
        f"is failed — and it is certainly not a quota wait — {body!r}"
    )
    assert body["stop_reason"] == "process_died_unexpectedly", (
        f"the interface does not say why the run ended: {body!r}"
    )
    assert body["ended_at"] is not None, (
        f"a failed execution has ended, so it must carry an end time: "
        f"{body!r}"
    )
    assert body["pid"] is None, (
        f"the interface still reports pid {body['pid']!r} for a run whose "
        f"process is gone — a pid reads as alive to every consumer"
    )

    row = _execution_row(plan_id)
    assert row["exec_status"] == "failed", (
        f"the row still says {row['exec_status']!r} although the process "
        f"is gone: {row!r}"
    )
    assert row["stop_reason"] == "process_died_unexpectedly", (
        f"the row does not record why the run ended: {row!r}"
    )
    assert row["exec_pid"] is None, (
        f"the row still carries pid {row['exec_pid']!r} for a run whose "
        f"process is gone: {row!r}"
    )
    # The row's own stamp is the durable record of the verdict.  The
    # table has no end-time column of its own, so the write that
    # carried the verdict is stamped with the moment it was written —
    # and that stamp is the end time every reader is given, before a
    # restart and after it alike.
    assert row["updated_at"] is not None, (
        f"the row carries no persistence stamp, so a reader cannot tell a "
        f"persisted verdict from a guessed one: {row!r}"
    )
    assert body["ended_at"] == row["updated_at"], (
        f"the end time differs between the interface ({body['ended_at']!r}) "
        f"and the row ({row['updated_at']!r}) — one verdict, one timestamp"
    )
    # And the process that reached the verdict records when it
    # persisted it, so the verdict in flight and the verdict on the row
    # are the same verdict.
    verdict = server._execution_state.get(plan_id) or {}
    assert verdict.get("verdict_persisted_at") is not None, (
        f"the verdict record carries no persistence stamp: {verdict!r}"
    )
    assert verdict["verdict_persisted_at"] == row["updated_at"], (
        f"the verdict was persisted at {verdict['verdict_persisted_at']!r} "
        f"but the row was stamped {row['updated_at']!r} — two verdicts, "
        f"two timestamps"
    )

    # --- 2. the verdict replays for a reader that holds nothing ---------
    server._execution_state.clear()
    with TestClient(server.app) as client:
        # This process holds nothing about the plan — it never started
        # the run.  The row is all there is.
        server._execution_state.clear()
        resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    replay = resp.json()
    assert replay["status"] == "failed", (
        f"the crash reads as {replay['status']!r} for a reader that only "
        f"has the row — the verdict did not replay: {replay!r}"
    )
    assert replay["stop_reason"] == "process_died_unexpectedly", (
        f"the replayed verdict lost its reason: {replay!r}"
    )
    assert replay["ended_at"] == row["updated_at"], (
        f"the replayed verdict carries a different end time "
        f"({replay['ended_at']!r} vs {row['updated_at']!r}) — the reader "
        f"re-derived it instead of reading the row"
    )
    assert replay["pid"] is None, (
        f"the replayed verdict still reports pid {replay['pid']!r}: "
        f"{replay!r}"
    )

    # --- 3. no row means "not started", never a fabricated failure ------
    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{VP009_NO_ROW_PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    empty = resp.json()
    assert empty["status"] == "not_started", (
        f"a plan with no execution row reads as {empty['status']!r}; "
        f"there is no row, so the run never started — {empty!r}"
    )
    assert empty["stop_reason"] is None, (
        f"a plan that never ran carries a reason: {empty!r}"
    )

    # --- 4. a parked run never enters the crash path --------------------
    parked_dir = crashed_execution.plans_dir / VP009_PARKED_PLAN_ID
    parked_dir.mkdir(parents=True)
    _seed_execution_row(
        VP009_PARKED_PLAN_ID,
        exec_status="waiting_for_quota",
        stop_reason="waiting_for_quota",
        exec_pid=_dead_pid(),
        project_dir=str(crashed_execution.project_dir),
        started_at="2026-10-24T15:00:00",
    )

    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{VP009_PARKED_PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    parked = resp.json()
    assert parked["status"] == "waiting_for_quota", (
        f"a parked run reads as {parked['status']!r}; only a ``running`` "
        f"row may be re-judged, so a quota pause is never a crash — "
        f"{parked!r}"
    )
    assert parked["stop_reason"] == "waiting_for_quota", (
        f"the parked run lost its reason: {parked!r}"
    )
    parked_row = _execution_row(VP009_PARKED_PLAN_ID)
    assert parked_row["exec_status"] == "waiting_for_quota", (
        f"the parked row was re-judged as {parked_row['exec_status']!r}: "
        f"{parked_row!r}"
    )
    assert parked_row["stop_reason"] == "waiting_for_quota", (
        f"the parked row lost its reason: {parked_row!r}"
    )

# ---------------------------------------------------------------------------
# 4. The verdict is persisted **with a timestamp**
# ---------------------------------------------------------------------------


def _wait_for_next_whole_second() -> None:
    """Sleep until the wall clock has moved into the next whole second.

    ``ExecutionRepository`` stamps ``updated_at`` with the current time
    **truncated to the second** (``_now_iso`` drops the microseconds),
    so two writes inside one second are indistinguishable.  For the
    test below to say anything about "the verdict was stamped when it
    was reached", the seed's write and the verdict's write have to be
    separable — so the test waits for the boundary rather than
    pretending two writes in the same second are two events.
    """
    import time
    from datetime import datetime, timezone

    start = datetime.now(tz=timezone.utc).replace(microsecond=0)
    while datetime.now(tz=timezone.utc).replace(microsecond=0) <= start:
        time.sleep(0.02)


def test_crash_verdict_is_persisted_with_a_timestamp(
    crashed_execution: SimpleNamespace,
) -> None:
    """The crash verdict lands on the row **stamped**, and is replayed with it.

    The sibling tests above pin *which* verdict the crash produces and
    that every reader agrees on it.  They do not pin *when* it was
    written — and that is the half that makes a verdict replayable
    rather than re-derived:

      * the write that carried the verdict stamped the row, so the row
        answers "when did this run end" with the moment the verdict was
        reached — not with the moment the run started, and not with a
        constant;
      * the stamp is a real timestamp, not a placeholder;
      * every reader is handed **that** stamp — the interface's end
        time, this process's own verdict record, and the row's own
        stamp are one value;
      * and a reader that holds nothing but the row (the next boot)
        is handed the same stamp, instead of a second guess at when the
        run stopped.
    """
    plan_id = crashed_execution.plan_id

    # The row before the verdict: the seed's own stamp.
    seeded = _execution_row(plan_id)
    assert seeded is not None, "the fixture did not seed an execution row"
    seed_stamp = seeded["updated_at"]
    assert seed_stamp is not None, (
        f"the seeded row carries no persistence stamp: {seeded!r}"
    )

    # Two writes in the same second carry the same stamp, so the test
    # waits for the boundary before the verdict is reached.
    _wait_for_next_whole_second()

    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed", (
        f"the crash reads as {body['status']!r}; a process that died is "
        f"failed — {body!r}"
    )

    row = _execution_row(plan_id)
    stamp = row["updated_at"]
    assert stamp is not None, (
        f"the row carries no persistence stamp, so a reader cannot tell a "
        f"persisted verdict from a guessed one: {row!r}"
    )
    assert stamp > seed_stamp, (
        f"the verdict write did not stamp the row: the row still carries "
        f"the seed's stamp {stamp!r} (the seed was {seed_stamp!r})"
    )
    assert stamp != row["started_at"], (
        f"the row's stamp is the run's start time {stamp!r} — the stamp "
        f"must say when the verdict was reached, not when the run began: "
        f"{row!r}"
    )
    # A real timestamp, in the format the repository writes.
    from datetime import datetime

    parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, (
        f"the stamp {stamp!r} carries no timezone — a reader cannot place "
        f"it on a timeline"
    )

    # One verdict, one timestamp: the interface, this process's record
    # and the row all say the same thing.
    verdict = server._execution_state.get(plan_id) or {}
    assert body["ended_at"] == stamp, (
        f"the interface's end time ({body['ended_at']!r}) is not the row's "
        f"stamp ({stamp!r}) — one verdict, one timestamp"
    )
    assert verdict.get("verdict_persisted_at") == stamp, (
        f"the verdict in flight was persisted at "
        f"{verdict.get('verdict_persisted_at')!r} but the row was stamped "
        f"{stamp!r} — two verdicts, two timestamps"
    )

    # And the stamp is **replayed**, not re-derived: a reader that
    # holds nothing but the row is handed the same moment.
    server._execution_state.clear()
    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    replay = resp.json()
    assert replay["ended_at"] == stamp, (
        f"after the restart the end time is {replay['ended_at']!r} but the "
        f"verdict was stamped {stamp!r} — the reader re-derived it"
    )


# ---------------------------------------------------------------------------
# 5. The answer is read from the row — and it says where it came from
# ---------------------------------------------------------------------------

#: A plan that exists only as a row: no plan directory, so the boot-time
#: recovery scan never restores it into the in-memory registry.
ROW_ONLY_PLAN_ID = "20261024-row-only-plan"

#: A plan nobody ever ran: no row at all.
NO_ROW_PLAN_ID = "20261024-no-row-at-all"

#: The provenance values the status answer carries.  ``state_db`` means
#: the answer was assembled from the durable ``plan_execution`` row;
#: ``default`` means the library really holds no row and the answer is
#: the empty default.
SOURCE_STATE_DB = "state_db"
SOURCE_DEFAULT = "default"


@pytest.fixture
def row_only_execution(tmp_path: Path) -> SimpleNamespace:
    """A plan that exists only as a row — no plan directory at all.

    The boot-time recovery scan walks the plans *tree*, so a plan with
    no directory is never restored into the in-memory registry.  That
    is exactly the shape a read has to survive: a fresh process that
    never saw the run, another process serving for the plan, or a plan
    whose directory was never created.

    The in-memory registry is the map that lives in the process that
    started the run.  It is empty everywhere else — and a reader that
    falls back to it alone answers ``not_started`` with every field
    null, which is how a dying run was reported as a plan that never
    ran.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # The database the seed lands in must be this test's own — never
    # the operator's live state.db.
    db_path = server._state_db_path()
    assert str(db_path).startswith(str(tmp_path)), (
        f"the state database resolves to {db_path}, which is outside the "
        f"test's tmp_path — the seed would land in a real database"
    )

    return SimpleNamespace(
        plan_id=ROW_ONLY_PLAN_ID,
        plans_dir=tmp_path / "plans",
        project_dir=project_dir,
    )


def test_the_status_answer_is_read_from_the_state_db_row(
    row_only_execution: SimpleNamespace,
) -> None:
    """A plan that only exists as a row is answered from that row.

    The row is the durable record of a run; the in-memory registry is
    one process's view of it.  A reader that holds nothing — a fresh
    boot, another process — must therefore answer from the row, and it
    must *say* that it did: ``data_source`` names where the answer came
    from, so an empty answer can never be mistaken for a persisted one.

    The row here carries a terminal verdict, so the answer is that
    verdict, in the row's own words: its status, its reason, its end
    time (the row's own stamp) and no pid (the executor is gone, and a
    pid reads as alive to every consumer).
    """
    plan_id = row_only_execution.plan_id
    _seed_execution_row(
        plan_id,
        exec_status="failed",
        stop_reason="process_died_unexpectedly",
        exec_pid=None,
        project_dir=str(row_only_execution.project_dir),
        started_at="2026-10-24T15:00:00",
    )
    row = _execution_row(plan_id)

    server._execution_state.clear()
    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed", (
        f"the row says failed but the interface says {body['status']!r}: "
        f"{body!r}"
    )
    assert body["stop_reason"] == "process_died_unexpectedly", (
        f"the interface did not carry the row's reason over: {body!r}"
    )
    assert body["ended_at"] == row["updated_at"], (
        f"the end time ({body['ended_at']!r}) is not the row's own stamp "
        f"({row['updated_at']!r}) — the reader re-derived it: {body!r}"
    )
    assert body["pid"] is None, (
        f"the interface reports pid {body['pid']!r} for a run whose "
        f"process is gone: {body!r}"
    )
    assert body.get("data_source") == SOURCE_STATE_DB, (
        f"the answer does not say it came from the state database: "
        f"{body!r}"
    )


def test_a_running_row_whose_process_is_gone_is_still_failed(
    row_only_execution: SimpleNamespace,
) -> None:
    """A row that says ``running`` with a dead process is ``failed``.

    The negative control of this round.  The row claims the run is
    going, but the process it names is gone — and nothing about it
    says "waiting for quota", so there is no capacity fact that could
    justify a wait.  The verdict is the crash verdict:
    ``failed`` / ``process_died_unexpectedly``, and it must be written
    to the row so that the next reader replays it instead of
    re-deriving it.

    The plan has no directory, so the boot-time scan never saw it and
    the in-memory registry is empty.  The read layer therefore has to
    hand the row to the standing liveness rule — the very same rule the
    watchdog and the boot scan apply — instead of reporting "running"
    forever, and never "not started".
    """
    plan_id = row_only_execution.plan_id
    dead_pid = _dead_pid()
    _seed_execution_row(
        plan_id,
        exec_status="running",
        exec_pid=dead_pid,
        project_dir=str(row_only_execution.project_dir),
        started_at="2026-10-24T15:00:00",
    )

    server._execution_state.clear()
    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed", (
        f"a row whose process is gone reads as {body['status']!r}; a "
        f"process that died is failed — and it is certainly not a quota "
        f"wait, and certainly not a run that never started: {body!r}"
    )
    assert body["status"] != "not_started", (
        f"the run was washed into 'never started': {body!r}"
    )
    assert body["stop_reason"] == "process_died_unexpectedly", (
        f"the interface does not say why the run ended: {body!r}"
    )
    assert body["pid"] is None, (
        f"the interface still reports pid {body['pid']!r} for a run whose "
        f"process is gone — a pid reads as alive to every consumer: "
        f"{body!r}"
    )
    assert body.get("data_source") == SOURCE_STATE_DB, (
        f"the answer does not say it came from the state database: "
        f"{body!r}"
    )

    # And the verdict was *written*, so the next reader replays it.
    row = _execution_row(plan_id)
    assert row["exec_status"] == "failed", (
        f"the row still says {row['exec_status']!r} although the process "
        f"is gone: {row!r}"
    )
    assert row["stop_reason"] == "process_died_unexpectedly", (
        f"the row does not record why the run ended: {row!r}"
    )
    assert row["exec_pid"] is None, (
        f"the row still carries pid {row['exec_pid']!r} for a run whose "
        f"process is gone: {row!r}"
    )


def test_a_plan_with_no_row_reads_as_not_started_from_the_default(
    row_only_execution: SimpleNamespace,
) -> None:
    """A plan with no row at all is ``not_started`` — and says so.

    The other half of the state.db-first read: the row is the truth
    about a run, and a plan that has no row has never run.  The answer
    must not invent a failure for it.

    The answer also has to *say* that it came from the default rather
    than from the row — that is what makes the field worth having: an
    empty answer and a persisted one must not look alike.
    """
    plan_id = NO_ROW_PLAN_ID
    _migrate_state_db()
    assert _execution_row(plan_id) is None, (
        "the fixture did not prepare a plan without a row"
    )

    server._execution_state.clear()
    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "not_started", (
        f"a plan with no execution row reads as {body['status']!r}; "
        f"there is no row, so the run never started: {body!r}"
    )
    assert body["stop_reason"] is None, (
        f"a plan that never ran carries a reason: {body!r}"
    )
    assert body.get("data_source") == SOURCE_DEFAULT, (
        f"the answer does not say it came from the default rather than "
        f"from the state database: {body!r}"
    )


def test_the_row_wins_over_a_stale_registry(
    row_only_execution: SimpleNamespace,
) -> None:
    """The row wins over a registry entry that has gone stale.

    The in-memory registry is one process's view of a run, and it goes
    stale the moment anything else touches the same plan.  The row is
    the durable record, so the answer is made of the row — including
    the fields the stale view still gets wrong.

    The stale view here says ``running`` while the row says the run is
    over, and it carries a *different* start time: an answer built from
    the stale view would report a run that is still going, with the
    wrong start time, and no indication that it was guessing.
    """
    plan_id = row_only_execution.plan_id
    _seed_execution_row(
        plan_id,
        exec_status="failed",
        stop_reason="process_died_unexpectedly",
        exec_pid=None,
        project_dir=str(row_only_execution.project_dir),
        started_at="2026-10-24T15:00:00",
    )
    row = _execution_row(plan_id)

    server._execution_state[plan_id] = {
        "status": "running",
        "started_at": "2026-01-01T00:00:00",
        "ended_at": None,
        "pid": 4242,
        "stop_reason": None,
        "project_dir": "/nonexistent/stale",
        "logs": deque(maxlen=2000),
        "process": None,
    }

    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed", (
        f"the stale registry won over the row: the interface says "
        f"{body['status']!r} while the row says {row['exec_status']!r}: "
        f"{body!r}"
    )
    assert body["stop_reason"] == "process_died_unexpectedly", (
        f"the interface did not carry the row's reason over: {body!r}"
    )
    # The answer has to name the SAME INSTANT as the row's stamp, not
    # spell it the same way: a row written before the timezone fix holds
    # a naive local stamp, and the status endpoint answers it with the
    # offset attached, so the two spell one moment differently.  The
    # question this test asks is "did the ROW win over the stale
    # registry?", and comparing instants keeps that question intact —
    # the stale registry's stamp names a moment months away, so an
    # answer built from it still fails here.
    assert _instant(body["started_at"]) == _instant(row["started_at"]), (
        f"the start time ({body['started_at']!r}) is not the row's "
        f"({row['started_at']!r}) — the answer was built from the stale "
        f"registry: {body!r}"
    )
    assert body.get("data_source") == SOURCE_STATE_DB, (
        f"the answer does not say it came from the state database: "
        f"{body!r}"
    )


# ---------------------------------------------------------------------------
# 6. The round's service outlives its starter
# ---------------------------------------------------------------------------
#
# The round that produced this task could not be judged: the probe was
# made against a port that was not answering, and the service log showed
# that the instance had been shut down inside the round — the state
# database was closed, checkpointed and backed up
# (``state.db shutdown backup -> backups/state-db/state-….db``) and then
# opened again.  A probe that cannot connect says nothing about the
# plan, and a database that is closed and reopened inside a round is a
# database that can be read from two different snapshots.
#
# So the criterion is pinned here as a contract test, against a service
# that has outlived the process that started the run:
#
#   * the run crashed (the row still says ``running``, the process it
#     names is gone, and there is no wait marker anywhere), so the
#     verdict is the crash verdict — ``failed`` /
#     ``process_died_unexpectedly`` — and never a quota wait;
#   * the answer is ANSWERABLE: it is a real answer about a real run,
#     not a "not started" that nobody recorded;
#   * and it says WHERE it came from — the database by path and by
#     inode, the moment it was opened, the row it found and the
#     integrity check that ran before the row was trusted.  Without
#     that, an answer produced by one instance and an answer produced
#     by another look alike, and a reader cannot tell which database it
#     is being told about.


def _restart_the_round_service(db_path: Path) -> Path:
    """Shut the database down the way the server's shutdown does.

    The real handler checkpoints the WAL first — so the copy is
    self-contained — and then takes the backup the startup guard
    restores from.  Doing the same here makes the round's restart a
    fact rather than a comment: the file that survives the restart is
    the one the backup was taken from.

    Returns the backup that was written.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.startup_guard import backup_now

    conn = db_open(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    backup = backup_now(db_path)
    assert backup is not None and backup.exists(), (
        f"the shutdown backup was not written for {db_path}"
    )
    return backup


def test_a_failed_execution_stays_answerable_after_the_round_service_outlives_its_starter(
    crashed_execution: SimpleNamespace,
) -> None:
    """A crashed run is still ``failed`` after the round's service restarts.

    The negative control of the capacity-wait criterion, across the
    restart: a process that really died and carries no wait marker must
    be judged ``failed`` — ``process_died_unexpectedly`` — no matter
    which instance of the service answers, and no matter that the
    database was closed, checkpointed, backed up and opened again in
    between.

    The half that used to be missing is the *answerability*: the answer
    must be about the run that happened, and it must be attributable.
    A reader that gets ``not_started`` — the default nobody recorded —
    cannot tell a crash from a plan that never ran, and the reader
    that cannot tell them apart believes the wrong one.
    """
    plan_id = crashed_execution.plan_id
    db_path = server._state_db_path()

    # The negative control's premise: the plan carries NO wait marker.
    (crashed_execution.plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "T1", "status": "in_progress"}]}),
        encoding="utf-8",
    )

    # --- the starter is gone -------------------------------------------
    # The process that started the run holds nothing about it any
    # more.  What is left is the file.
    server._execution_state.clear()

    # --- the round's service is restarted ------------------------------
    # The database is closed, checkpointed and backed up, and then it is
    # opened again by the instance that answers.
    backup = _restart_the_round_service(db_path)
    assert backup.exists(), (
        f"the restart left no backup behind at {backup}"
    )

    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # --- the verdict ----------------------------------------------------
    assert body["status"] == "failed", (
        f"after the restart the crash reads as {body['status']!r}; a "
        f"process that died is failed — and it is certainly not a quota "
        f"wait, and certainly not a run that never started: {body!r}"
    )
    assert body["stop_reason"] == "process_died_unexpectedly", (
        f"after the restart the interface does not say why the run "
        f"ended: {body!r}"
    )
    assert body["pid"] is None, (
        f"after the restart the interface reports pid {body['pid']!r} for "
        f"a run whose process is gone — a pid reads as alive to every "
        f"consumer: {body!r}"
    )

    row = _execution_row(plan_id)
    assert row["exec_status"] == "failed", (
        f"the row still says {row['exec_status']!r} although the process "
        f"is gone: {row!r}"
    )
    assert row["stop_reason"] == "process_died_unexpectedly", (
        f"the row does not record why the run ended: {row!r}"
    )
    assert body["ended_at"] == row["updated_at"], (
        f"the end time differs between the interface ({body['ended_at']!r}) "
        f"and the row ({row['updated_at']!r}) — one verdict, one timestamp"
    )

    # --- the answer says where it came from ----------------------------
    assert body.get("data_source") == SOURCE_STATE_DB, (
        f"the answer does not say it came from the state database: "
        f"{body!r}"
    )
    detail = body.get("data_source_detail") or {}
    assert detail.get("state_db_path") == str(db_path), (
        f"the answer does not name the database it read: {detail!r}"
    )
    assert detail.get("state_db_inode") == db_path.stat().st_ino, (
        f"the answer names the database by a different inode than the "
        f"file on disk — it read somebody else's database: {detail!r}"
    )
    assert detail.get("opened_at"), (
        f"the answer does not say when it opened the database: {detail!r}"
    )
    assert detail.get("row_updated_at") == row["updated_at"], (
        f"the answer names row stamp {detail.get('row_updated_at')!r} "
        f"while the row carries {row['updated_at']!r} — the provenance "
        f"and the verdict do not describe the same row: {detail!r}"
    )
    assert detail.get("integrity") == "ok", (
        f"the read did not prove the database it trusted: {detail!r}"
    )


def _seed_a_routing_row_for_another_plan(plan_id: str) -> None:
    """Give the library a row of its own — for a plan that is not ours.

    The refusal is about a library that RECORDS runs and has never
    heard of the plan in question.  An empty library says something
    else entirely ("this instance has never recorded anything"), so
    the fixture has to put a row in before the refusal can be reached.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    conn = db_open(server._state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "ready")
    finally:
        conn.close()


def test_a_plan_the_library_cannot_see_is_refused_instead_of_read_as_not_started(
    tmp_path: Path,
) -> None:
    """A plan the library cannot see is refused, not answered "not started".

    The other half of the same rule.  The library was read — it opened,
    it is intact, it holds rows of its own — and it holds no row for
    this plan: neither an execution row nor a routing row.  The plan's
    directory is right there in this instance's tree.

    "Not started" is a claim about the plan's history, and this instance
    cannot make it: the database it read does not describe the tree it
    serves.  That is the shape a stale snapshot takes inside a round
    that restarts the service, and the shape that made a dying run read
    as a plan that never ran.  So the answer is a refusal — 503 — that
    names the database that could not answer, rather than a default
    that nobody recorded.
    """
    plan_dir = server.PLANS_DIR / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    # The library exists and is readable — this is not a broken
    # database, it is a database that does not know this plan.  It
    # holds a row of its own (for another plan), because that is what
    # makes it a library that RECORDS runs.
    _migrate_state_db()
    _seed_a_routing_row_for_another_plan("20261024-some-other-plan")
    db_path = server._state_db_path()

    server._execution_state.clear()
    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 503, (
        f"a plan the library cannot see was answered {resp.status_code} "
        f"instead of being refused: {resp.text!r}"
    )
    body = resp.json()
    assert body.get("data_source") == SOURCE_STATE_DB, (
        f"the refusal does not say which database could not answer: "
        f"{body!r}"
    )
    detail = body.get("data_source_detail") or {}
    assert detail.get("state_db_path") == str(db_path), (
        f"the refusal does not name the database it read: {detail!r}"
    )
    assert detail.get("integrity") == "ok", (
        f"the refusal does not say the database was intact — so the "
        f"reader cannot tell a broken database from a foreign one: "
        f"{detail!r}"
    )


def test_a_plan_in_an_empty_library_is_answered_not_started(
    tmp_path: Path,
) -> None:
    """An instance that has recorded nothing may still say "not started".

    The other side of the refusal, and it is a deliberate one.  An
    EMPTY library is not a foreign one: this instance has never
    recorded a run, so there is nothing here that could describe a
    plan's history and nothing here that contradicts the plan's
    directory either.  The claim "this plan never ran" is the only
    claim an instance with no records can make, and it is an honest
    one.

    The refusal is for the opposite case — a library that RECORDS runs
    and has never heard of this plan.  Confusing the two would turn
    every fresh install into a wall of 503s, and would stop the
    endpoint from answering the question it exists to answer.
    """
    plan_dir = server.PLANS_DIR / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    # The library exists, it is readable, and it holds no row at all.
    _migrate_state_db()
    db_path = server._state_db_path()

    server._execution_state.clear()
    with TestClient(server.app) as client:
        resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, (
        f"an instance that has recorded nothing refused to answer a plan "
        f"it knows nothing about: {resp.text!r}"
    )
    body = resp.json()
    assert body["status"] == "not_started", (
        f"a plan with no row at all reads as {body['status']!r}: {body!r}"
    )
    assert body["stop_reason"] is None, (
        f"a plan that never ran carries a reason: {body!r}"
    )
    detail = body.get("data_source_detail") or {}
    assert detail.get("state_db_path") == str(db_path), (
        f"the answer does not name the database it read: {detail!r}"
    )

