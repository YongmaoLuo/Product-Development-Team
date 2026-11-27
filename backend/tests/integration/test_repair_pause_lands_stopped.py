"""A repair round that was PAUSED must land ``stopped``, not ``failed``.

2026-11-20 — arch decision R1 / test decision R7
-------------------------------------------------

The scene (production, 2026-11-20 08:42): the operator paused a plan
while a repair round was in flight.  ``pause_plan`` wrote the pause
intent, ``stop_verification`` SIGTERMed the repair executor, and the
executor came back with ``rc=-15``.  The repair exit block read that
return code as a crash and wrote ``failed`` into ``plan_execution`` —
so a plan the operator had paused came back as a *failed* plan, with
nothing on disk saying a pause had ever been asked for.

The exit code cannot tell the two apart: the SIGTERM the pause's own
kill sends looks exactly like a crash.  The **pause intent on disk**
is what tells them apart — it is written *before* anything is stopped,
which is the only reason it is still there when the exit block runs.

These cases pin the contract:

* an intent on disk + ``rc=-15`` → the round lands ``stopped``;
* no intent + ``rc=-15`` → the round still lands ``failed`` (a crash
  must not be re-judged as a pause);
* a DAMAGED intent + ``rc=-15`` → still ``failed`` (a file that cannot
  be read says nothing about whether a pause was asked for), and the
  damaged file is not deleted — it is the only trace of the request;
* the intent is cleared only once the ``stopped`` status is on disk;
* a status write that FAILED keeps the intent, for the next round;
* a kill that did NOT land → the plan is still running, so the phase
  stays where it is and the reason says ``pause_kill_incomplete``.

A round that is not a pause is judged by the repair batch's
classification table, so its REASON is the table's verdict (here: the
repair task never ran, so the batch did not drain).  The killing
itself is recorded beside it, in ``repair_death`` — a fact about the
process, kept out of the verdict about the batch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import server
from pause_state import (
    PAUSE_INTENT_FILENAME,
    intent_path,
    write_pause_intent,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mock_subprocess(returncode: int = -15, pid: int = 424242):
    """A process handle that ended on ``returncode`` (a signal is negative)."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = pid
    proc.wait = MagicMock(return_value=returncode)
    return proc


def _seed_plan(tmp_path, plan_id, *, execution_row=True):
    """A plan directory plus (optionally) a ``plan_execution`` row."""
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "repair-r1-01"}]}), encoding="utf-8",
    )

    if not execution_row:
        return plan_dir

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(server._state_db_path())
    try:
        migrate(conn)
        ExecutionRepository(conn).update_fields(
            plan_id,
            create_if_missing=True,
            exec_status="running",
            exec_pid=424242,
        )
    finally:
        conn.close()
    return plan_dir


def _exec_status(plan_id):
    """The ``exec_status`` the ``plan_execution`` row carries."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(server._state_db_path())
    try:
        row = ExecutionRepository(conn).summary(plan_id) or {}
    finally:
        conn.close()
    return row.get("exec_status")


def _run_repair(monkeypatch, tmp_path, plan_id, returncode=-15):
    """Drive ``_run_repair_execution_async`` to completion; return its state."""
    monkeypatch.setattr(
        server, "_plan_dir", lambda pid: tmp_path / "plans" / pid,
    )
    monkeypatch.setattr(
        server,
        "_spawn_executor_subprocess",
        lambda **kw: (
            _mock_subprocess(returncode=returncode),
            tmp_path / "repair.log",
        ),
    )
    with server._execution_state_lock:
        server._execution_state.clear()

    done = threading.Event()

    def _cb(rc):
        done.set()

    server._run_repair_execution_async(plan_id, tmp_path, on_complete=_cb)
    assert done.wait(timeout=30.0), "on_complete never fired"
    with server._execution_state_lock:
        return dict(server._execution_state[plan_id])


# ---------------------------------------------------------------------------
# the contract
# ---------------------------------------------------------------------------


def test_sigterm_with_intent_lands_stopped(monkeypatch, tmp_path):
    """A pause that was asked for lands ``stopped`` — never ``failed``.

    The executor was SIGTERMed by the pause's own kill, so its exit
    code is ``-15``.  That code alone says nothing; the intent on disk
    says the pause was asked for.
    """
    plan_id = "p-pause-with-intent"
    plan_dir = _seed_plan(tmp_path, plan_id)
    write_pause_intent(plan_dir, reason="user_pause")

    state = _run_repair(monkeypatch, tmp_path, plan_id, returncode=-15)

    assert state["status"] == "stopped", (
        "a repair round that was paused was recorded as "
        f"{state['status']!r} — the operator's pause came back as a crash"
    )
    assert state["stop_reason"] == "paused", state
    assert _exec_status(plan_id) == "stopped", (
        "the plan_execution row still says the round failed"
    )


def test_sigterm_without_intent_still_failed(monkeypatch, tmp_path):
    """No intent on disk → the round is a crash, and stays one.

    The other side of the same door: the pause check must not become a
    way to re-judge an ordinary failure as a pause.

    The crash's DEATH is recorded beside the batch's OUTCOME, not on
    top of it: the outcome is the classification table's verdict (here
    "the repair task never ran"), and the death says what happened to
    the process.
    """
    plan_id = "p-pause-no-intent"
    _seed_plan(tmp_path, plan_id)

    state = _run_repair(monkeypatch, tmp_path, plan_id, returncode=-15)

    assert state["status"] == "failed", state
    assert state["stop_reason"] == "repair_batch_not_drained:1_pending", state
    assert state["repair_death"] == "repair_signal:15", state
    assert _exec_status(plan_id) == "failed", (
        "a crash with no pause intent on disk was recorded as stopped"
    )


def test_intent_cleared_after_stop(monkeypatch, tmp_path):
    """The intent is cleared once the ``stopped`` status is on disk.

    The intent is the evidence that a pause was asked for; once the
    pause has been honoured and the status says ``stopped``, keeping
    it would make the NEXT round read a pause that no longer applies.
    """
    plan_id = "p-pause-intent-cleared"
    plan_dir = _seed_plan(tmp_path, plan_id)
    write_pause_intent(plan_dir, reason="user_pause")
    assert intent_path(plan_dir).exists()

    state = _run_repair(monkeypatch, tmp_path, plan_id, returncode=-15)

    assert state["status"] == "stopped", state
    assert not intent_path(plan_dir).exists(), (
        "the pause intent survived the round that honoured it"
    )


def test_kill_incomplete_keeps_phase(monkeypatch, tmp_path):
    """A kill that did not land: the plan is still running, so it is NOT stopped.

    The repair executor's own children detach from it, so the executor's
    exit proves nothing about the rest of the plan's tree.  A survivor
    means the pause did not take — recording ``stopped`` then would be
    the same lie one level up: a plan that says "stopped" while it is
    still running.
    """
    plan_id = "p-pause-kill-incomplete"
    plan_dir = _seed_plan(tmp_path, plan_id)
    write_pause_intent(plan_dir, reason="user_pause")

    # A process of this plan the pause's kill did NOT take: the repair
    # executor's own child, still running when the executor itself died.
    survivor = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        import process_ledger

        process_ledger.record_spawn(
            plan_dir,
            process_ledger.ProcessRecord(
                plan_id=plan_id,
                role="subagent",
                pid=survivor.pid,
                pgid=os.getpgid(survivor.pid),
                create_time=time.time(),
                cmdline=("python", "-c", "sleep"),
                cwd=str(tmp_path),
                owner_pid=os.getpid(),
                owner_role="executor",
            ),
        )
        state = _run_repair(monkeypatch, tmp_path, plan_id, returncode=-15)
    finally:
        survivor.terminate()
        try:
            survivor.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover — defensive
            survivor.kill()

    assert state["stop_reason"] == "pause_kill_incomplete", state
    assert state["status"] != "stopped", (
        "the plan is still running — recording it as stopped is the lie "
        "this case exists to prevent"
    )
    assert _exec_status(plan_id) != "stopped", (
        "the plan_execution row says stopped while the plan still runs"
    )
    # The phase is left exactly where it was: the pause did not take,
    # so the plan did not go to ``stopped``.
    assert server.PlanState(plan_dir).get_current_phase() == "executing"
    # The intent survives: the pause has NOT been honoured yet, so it
    # is the evidence the next round needs.
    assert intent_path(plan_dir).exists(), (
        "the pause intent was cleared although the pause never took"
    )


@pytest.mark.parametrize(
    "torn_intent",
    [
        pytest.param("{not json at all", id="torn_json"),
        pytest.param('{"schema_version": 1}', id="reasonless"),
    ],
)
def test_corrupt_intent_still_failed(monkeypatch, tmp_path, torn_intent):
    """A DAMAGED intent file is no intent: the round stays a crash.

    The intent is the only evidence that a pause was asked for, and it
    is written before anything is stopped — which also means it can be
    torn by a crash of its own.  A file that cannot be read says
    nothing about whether a pause was asked for, so it is not allowed
    to re-judge a crash as a pause: ``read_pause_intent`` answers
    ``None`` for it and the round lands ``failed``, exactly as it would
    with no file at all.

    The other side of the same coin: the damaged file is NOT cleared.
    A pause that was asked for but whose record was damaged still
    deserves the next round's attention, so the file stays where it
    is — deleting it would throw away the only trace of the request.
    """
    plan_id = f"p-pause-corrupt-intent-{torn_intent}"
    plan_dir = _seed_plan(tmp_path, plan_id)
    intent_path(plan_dir).write_text(torn_intent, encoding="utf-8")

    state = _run_repair(monkeypatch, tmp_path, plan_id, returncode=-15)

    assert state["status"] == "failed", state
    assert state["stop_reason"] == "repair_batch_not_drained:1_pending", state
    assert state["repair_death"] == "repair_signal:15", state
    assert _exec_status(plan_id) == "failed", (
        "a crash with a damaged pause intent on disk was recorded as "
        "stopped — a damaged file is not evidence of a pause"
    )
    assert intent_path(plan_dir).exists(), (
        "the damaged pause intent was deleted — the next round has no "
        "record that a pause was ever asked for"
    )


def test_intent_kept_when_status_write_fails(monkeypatch, tmp_path):
    """A write that FAILED keeps the intent — the next round needs it.

    The intent is the only evidence that a pause was asked for.  Were
    it cleared before the status landed, a round whose write failed
    would leave nothing on disk at all: the next round would find no
    intent, call the crash a failure, and the pause would be lost.
    """
    plan_id = "p-pause-write-failed"
    plan_dir = _seed_plan(tmp_path, plan_id, execution_row=False)
    write_pause_intent(plan_dir, reason="user_pause")

    state = _run_repair(monkeypatch, tmp_path, plan_id, returncode=-15)

    # The in-memory verdict is still the pause's — only the WRITE failed.
    assert state["status"] == "stopped", state
    assert intent_path(plan_dir).exists(), (
        "the pause intent was cleared although the status never reached "
        "the database — the next round has no evidence of the pause"
    )