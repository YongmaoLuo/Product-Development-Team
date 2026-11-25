"""The pause intent reaches the disk before anything acts on the pause.

Background
----------
Pausing a plan is two things: the **intent** ("someone asked for this to
stop, and here is why") is recorded, and the round that is running is
stopped.  The order between them is the whole point of this file.

The thing that acts on the pause is a process that can be killed right
after it starts: the repair executor is stopped with SIGTERM and then
SIGKILL, and everything it had not yet written goes with it.  So the
evidence that a pause was asked for has to be on disk **before** the
stop -- a repair round that finds no intent on disk cannot tell "the
user paused this" from "this crashed", and answers "failed" for both.

What this pins
--------------
1. **The order** -- the intent is written before ``stop_plan`` is called.
   Reversing the two is the negative control the PRD asks for: the R1
   case has to go red when the order is reversed.
2. **A failed write stops the pause** -- if the intent cannot be
   written, the stop is not started at all: the answer says
   ``pause_intent_write_failed`` and the executor stays alive.
3. **An unknown plan writes nothing** -- the answer stays the existing
   ``no_such_plan``, and neither a directory nor a file appears.
4. **A resume clears the intent** -- the pause was honoured, so the
   evidence is retired with it.

No executor, no server: the facade is driven against a temp plans tree
and the real SQLite state database, which is where the phases live.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, Iterator, List

import pytest

#: The file the intent lives in (``pause_state.PAUSE_INTENT_FILENAME``).
PAUSE_INTENT_FILENAME = "pause_intent.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plans_root() -> Path:
    """The plans tree this case writes into.

    The suite redirects ``server.PLANS_DIR`` at ``tmp_path`` -- the facade
    resolves a plan through ``server._plan_dir``, so the case has to write
    where the facade looks.
    """
    import server

    return Path(server.PLANS_DIR)


def _make_plan(plan_id: str, phase: str) -> Path:
    """Create a plan directory whose phase is ``phase``.

    The phase is written as the legacy ``plan_state.json`` snapshot:
    ``PlanState`` reads it when there is no routing row yet, which is the
    same path a plan bootstrapped by the API takes.
    """
    plan_dir = _plans_root() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({"plan_id": plan_id, "current_phase": phase}),
        encoding="utf-8",
    )
    return plan_dir


def _intent_path(plan_dir: Path) -> Path:
    """Where the intent file of ``plan_dir`` lives."""
    return plan_dir / PAUSE_INTENT_FILENAME


def _read_intent(plan_dir: Path) -> Any:
    """What the intent file on disk actually says."""
    return json.loads(_intent_path(plan_dir).read_text(encoding="utf-8"))


@contextlib.contextmanager
def _command_executor() -> Iterator[Any]:
    """A scheduler command executor over this case's database.

    The collaborators are the production ones -- the store, the capacity
    ledger, the facts reader and the lifecycle facade.  Only the
    connection is the case's own.
    """
    import server
    from plan_lifecycle_service import PlanLifecycleService
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.commands import CommandExecutor
    from plan_scheduler.facts import PlanFactsReader
    from plan_scheduler.store import SqliteSchedulerStore
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(server._state_db_path())
    try:
        migrate(conn)
        store = SqliteSchedulerStore()
        facts = PlanFactsReader(conn, _plans_root())
        ledger = CapacityLedger(store, facts)
        yield CommandExecutor(
            store,
            ledger,
            PlanLifecycleService(),
            facts=facts,
            conn=conn,
        )
    finally:
        conn.close()


def _stopped_result(plan_id: str) -> Any:
    """The answer the stop path gives when it really stopped something."""
    from plan_lifecycle_service import LifecycleResult

    return LifecycleResult(
        plan_id=plan_id,
        dispatched="execution",
        berth_phase="stopped",
        final_phase="stopped",
        checkpoint=None,
        detail="stop_execution",
    )


# ---------------------------------------------------------------------------
# 1. The intent is written before the stop starts
# ---------------------------------------------------------------------------


def test_intent_written_before_stop(monkeypatch):
    """The intent is on disk before ``stop_plan`` is called.

    The order is the contract: the stop can kill the process that would
    have written the intent afterwards, so the intent goes first.  The
    call order is recorded rather than inferred from the file's presence
    -- a file that happens to exist proves nothing about *when* it was
    written.
    """
    import pause_state
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "pause-intent-order-p1"
    plan_dir = _make_plan(plan_id, "executing")

    order: List[str] = []
    real_write = pause_state.write_pause_intent

    def recording_write(plan_dir_arg, *, reason):
        order.append("write")
        return real_write(plan_dir_arg, reason=reason)

    def recording_stop(self, pid, requeue=False):
        order.append("stop")
        return _stopped_result(pid)

    monkeypatch.setattr(pause_state, "write_pause_intent", recording_write)
    monkeypatch.setattr(PlanLifecycleService, "stop_plan", recording_stop)

    result = PlanLifecycleService().pause_plan(plan_id)

    assert order == ["write", "stop"], (
        f"the intent must be on disk before the stop starts: {order!r}"
    )
    # And what was written is the intent, with the reason the pause had.
    assert _intent_path(plan_dir).is_file()
    assert _read_intent(plan_dir)["reason"] == "user_pause"
    assert result.ok is True


# ---------------------------------------------------------------------------
# 2. A write that fails stops the pause before it starts
# ---------------------------------------------------------------------------


def test_write_failure_aborts_stop(monkeypatch):
    """An intent that cannot be written aborts the pause.

    The pause is two steps, and the first one failed: the round is
    still running, so the executor has to stay alive and the caller has
    to be told why nothing happened.  Starting the stop anyway would
    kill a round whose "someone asked for this" is not on record -- the
    very state the repair round needs to tell a pause from a crash.
    """
    import pause_state
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "pause-intent-write-fails-p2"
    plan_dir = _make_plan(plan_id, "executing")

    stop_calls: List[str] = []

    def failing_write(plan_dir_arg, *, reason):
        raise OSError("disk is full")

    def counting_stop(self, pid, requeue=False):
        stop_calls.append(pid)
        return _stopped_result(pid)

    monkeypatch.setattr(pause_state, "write_pause_intent", failing_write)
    monkeypatch.setattr(PlanLifecycleService, "stop_plan", counting_stop)

    result = PlanLifecycleService().pause_plan(plan_id)

    assert stop_calls == [], (
        "the stop must not start when the intent is not on disk: "
        f"{stop_calls!r}"
    )
    assert result.ok is False
    assert result.detail == "pause_intent_write_failed"
    assert not _intent_path(plan_dir).exists()


# ---------------------------------------------------------------------------
# 3. An unknown plan writes nothing
# ---------------------------------------------------------------------------


def test_unknown_plan_writes_nothing():
    """A plan that is not there is refused, and nothing is written.

    The answer stays the existing one -- ``no_such_plan`` -- and the
    refusal leaves no trace on disk: no directory, no intent.  A pause
    for a plan that does not exist is a typo, and a typo must not leave
    an intent behind that a later repair round would read as "this was
    paused".
    """
    from plan_lifecycle_service import PlanLifecycleService, PlanNotFoundError

    plan_id = "pause-intent-unknown-p3"
    plan_dir = _plans_root() / plan_id
    assert not plan_dir.exists()

    with pytest.raises(PlanNotFoundError):
        PlanLifecycleService().pause_plan(plan_id)

    assert not plan_dir.exists()
    assert not _intent_path(plan_dir).exists()

    # The answer the operator gets is the existing one.
    with _command_executor() as executor:
        result = executor.pause(plan_id)

    assert result.outcome == "not_found"
    assert result.detail == "no_such_plan"
    assert not plan_dir.exists()


# ---------------------------------------------------------------------------
# 4. A resume retires the intent
# ---------------------------------------------------------------------------


def test_requeue_clears_intent():
    """A requeued plan has no intent left: the pause was honoured.

    The intent says "someone asked for this to stop".  Once the plan is
    back in the queue that question has been answered, and a stale
    intent would make the next crash look like a pause.
    """
    import pause_state
    from plan_lifecycle_service import PlanLifecycleService

    plan_id = "pause-intent-requeue-p4"
    plan_dir = _make_plan(plan_id, "stopped")
    pause_state.write_pause_intent(plan_dir, reason="user_pause")
    assert _intent_path(plan_dir).is_file()

    result = PlanLifecycleService().requeue_plan(plan_id)

    assert result.ok is True
    assert not _intent_path(plan_dir).exists()
