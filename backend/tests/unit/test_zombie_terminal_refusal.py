"""A round that was declared dead must not write the plan's terminal state.

Background
----------
The watchdog can declare a verification round dead while the round is
still running: the log went silent past the hard ceiling, the VP
subprocesses were killed, and the plan was written off.  The round
itself does not stop, though.  Its LLM call returns, its code carries
on down the normal path, and it writes a terminal state, moves the
plan's phase and pushes a card -- for a round the plan has already
moved past.

The durable mark is the round's own ``round_token`` (see
``backend/zombie_rounds.py``): a round that was declared dead is
recorded in the plan directory, and the code that might wake up asks
``is_invalidated(plan_dir, round_token)`` before it acts.

This file pins the CONSUMER side of that mark: the terminal write of an
invalidated round is REFUSED.

Contract pinned here
--------------------
* ``_persist_verification_terminal`` takes the round's token and refuses
  the whole write when that token was declared dead:
  ``plan_verification`` unchanged, ``plan_routing.current_phase``
  unchanged, no ``KIND_PLAN_CLOSED`` on the state bus.
* A live round's terminal write is unaffected -- the refusal is about
  the DEAD round, not about terminal writes in general.
* A caller with no token (the watchdog itself, which is the thing that
  declares the death) writes as before.
* The loop's own ``_record_terminal`` closure carries the token it was
  BUILT with -- the token of the round it belongs to -- and not the
  token the plan's in-memory state happens to carry when the write
  happens.  A newer round overwrites that state, so reading it at
  write time would let a dead round's verdict land under a live round's
  name.
* The repair-generation failure recorder is bound to the round too: the
  module-level recorder is called by name, so the loop has to bind the
  token itself.

Hermeticity
------------
Everything happens inside ``tmp_path``: the plan directory, the project
directory and the state database.  ``PDT_STATE_DB_PATH`` is the seam
``server._state_db_path`` AND ``plan_state._state_db_path`` both
honour, so no write can escape to the operator's live database.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server``
# imports back, so importing the inner one first trips the cycle.
import server as srv  # noqa: E402
import verification_loop as vl  # noqa: E402
import zombie_rounds  # noqa: E402


#: The token of the round that was declared dead.
DEAD_TOKEN = "verification_loop-1-ab"

#: The token of a round that is still running.
LIVE_TOKEN = "verification_loop-2-cd"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def hermetic_server(tmp_path, monkeypatch):
    """A hermetic plans root + ``state.db``, and the in-memory state back."""
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    db_path = tmp_path / "state.db"

    # The env var is the seam BOTH ``server._state_db_path`` and
    # ``plan_state._state_db_path`` honour, so the PlanState writes the
    # terminal path performs cannot escape to the live database.
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(srv, "PLANS_DIR", plans_root)
    monkeypatch.setattr(srv, "_state_db_path", lambda *a, **k: db_path)

    before_state = dict(srv._verification_state)
    before_exec = dict(srv._execution_state)
    yield srv, plans_root, db_path
    srv._verification_state.clear()
    srv._verification_state.update(before_state)
    srv._execution_state.clear()
    srv._execution_state.update(before_exec)


def _seed_running_verification(
    db_path: Path, plan_id: str, plans_root: Path,
    phase: str = "verification_running",
) -> None:
    """Seed ``plan_routing`` + ``plan_verification`` mid-verification."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, substage, current_phase, version, completed_phases, "
            " review_rounds, flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, None, phase, 0, "[]", "{}", "{}",
                json.dumps(
                    {"status": "running", "round": 1, "max_rounds": 3,
                     "stop_reason": None}
                ),
                "2026-11-01T00:00:00", "2026-11-01T00:00:00Z",
            ),
        )
        conn.commit()
        repo = VerificationRepository(conn)
        repo.insert(plan_id, "running")
        repo.init_round(plan_id, round_n=1, max_rounds=3)
    finally:
        conn.close()

    (plans_root / plan_id).mkdir(parents=True, exist_ok=True)


def _phase(db_path: Path, plan_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _verification_status(db_path: Path, plan_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT verification_status FROM plan_verification "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _declare_dead(plan_dir: Path, round_token: str) -> None:
    """Mark ``round_token`` as declared dead for this plan."""
    zombie_rounds.invalidate(
        plan_dir, round_token,
        reason="verification_log_stale", actor="verification_watchdog",
    )


class _PlanClosedRecorder:
    """Collect the ``plan_closed`` events the state bus publishes."""

    def __init__(self) -> None:
        self.events: List[Any] = []

    def __call__(self, event: Any) -> None:
        self.events.append(event)

    def for_plan(self, plan_id: str) -> List[Any]:
        from notifications.state_events import KIND_PLAN_CLOSED

        return [
            event for event in self.events
            if event.kind == KIND_PLAN_CLOSED and event.plan_id == plan_id
        ]


@pytest.fixture
def plan_closed_events():
    """Subscribe to the state bus for the duration of one test."""
    from notifications.state_events import STATE_EVENT_BUS

    recorder = _PlanClosedRecorder()
    STATE_EVENT_BUS.subscribe(recorder)
    try:
        yield recorder
    finally:
        STATE_EVENT_BUS.unsubscribe(recorder)


class _FakePlanState:
    """The plan's state machine, reduced to what the loop calls.

    The routing CAS (a SQL write on ``plan_routing``) is what this
    file measures; the PlanState mirror is not, and a real PlanState
    would write into the operator's plan tree.
    """

    def __init__(self, plan_dir: Path) -> None:
        self.plan_dir = plan_dir

    def ensure_row(self) -> None:
        # 2026-11-11: the loop asks for the routing row to exist before
        # its CAS (constructing a PlanState no longer creates it).
        pass

    def set_verification_max_rounds(self, max_rounds: int) -> None:
        pass

    def reload(self) -> None:
        pass

    def begin_verification(self, round_n=None) -> None:
        pass

    def transition_to(self, phase: str) -> None:
        pass

    def force_set_phase(self, phase: str) -> None:
        pass

    def get_current_phase(self) -> str:
        return "verification_running"


# ---------------------------------------------------------------------------
# 1. The terminal write of a dead round is refused
# ---------------------------------------------------------------------------


def test_terminal_write_refused_for_invalidated_round(hermetic_server):
    """A round that was declared dead must not write the terminal state.

    The write is refused WHOLE: the ``plan_verification`` verdict, the
    ``plan_routing`` phase and the closed event are three faces of one
    decision, and a dead round must not be able to make any of them.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "zombie-refused"
    _seed_running_verification(db_path, plan_id, plans_root)
    plan_dir = plans_root / plan_id
    _declare_dead(plan_dir, DEAD_TOKEN)

    server._persist_verification_terminal(
        plan_id, "failed", "repair_exhausted", round_token=DEAD_TOKEN,
    )

    assert _verification_status(db_path, plan_id) == "running", (
        "a round that was declared dead wrote its verdict into "
        "plan_verification — the plan now reports a verdict for a round "
        "it has already written off"
    )
    assert _phase(db_path, plan_id) == "verification_running", (
        "a round that was declared dead moved the plan's phase — the plan "
        "is now terminal for a round that was already over"
    )


def test_terminal_write_persists_for_a_live_round(hermetic_server):
    """The anti-vacuity control: a LIVE round still writes its terminal.

    Without this, "nothing was written" would be true for the wrong
    reason -- a terminal write that never lands is not a refusal.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "zombie-live-round"
    _seed_running_verification(db_path, plan_id, plans_root)
    plan_dir = plans_root / plan_id

    # The token is a real one -- the round is running -- and it was
    # never declared dead.
    server._persist_verification_terminal(
        plan_id, "failed", "repair_exhausted", round_token=LIVE_TOKEN,
    )

    assert _verification_status(db_path, plan_id) == "failed", (
        "a live round's terminal write did not land — the refusal is "
        "supposed to be about DEAD rounds, not about terminal writes"
    )
    assert _phase(db_path, plan_id) == "failed", (
        "a live round's terminal write did not advance the plan's phase"
    )


def test_terminal_write_without_a_token_still_persists(hermetic_server):
    """``round_token=None`` is the watchdog's own write.

    The watchdog is the thing that DECLARES the death -- it is the
    authority that writes the terminal state for a round it has just
    killed.  A caller that names no round is a caller the refusal does
    not apply to.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "zombie-no-token"
    _seed_running_verification(db_path, plan_id, plans_root)

    server._persist_verification_terminal(
        plan_id, "failed", "verification_log_stale", chain_ending=True,
    )

    assert _verification_status(db_path, plan_id) == "failed", (
        "a caller that named no round lost its terminal write — the "
        "watchdog's own write must still land"
    )
    assert _phase(db_path, plan_id) == "failed"


def test_terminal_write_for_another_dead_round_is_refused(
    hermetic_server,
):
    """The refusal is per-ROUND, not per-plan.

    A plan can hold the marks of several dead rounds at once.  Only the
    round that was declared dead is refused; a different round's write
    is not.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "zombie-other-round"
    _seed_running_verification(db_path, plan_id, plans_root)
    plan_dir = plans_root / plan_id
    _declare_dead(plan_dir, DEAD_TOKEN)

    server._persist_verification_terminal(
        plan_id, "failed", "repair_exhausted", round_token=LIVE_TOKEN,
    )

    assert _verification_status(db_path, plan_id) == "failed", (
        "a different round's terminal write was refused — the mark names "
        "ONE round, and the plan may have several dead ones"
    )


# ---------------------------------------------------------------------------
# 2. The loop's own recorder
# ---------------------------------------------------------------------------


def test_record_terminal_does_not_change_phase_after_invalidation(
    hermetic_server, monkeypatch,
):
    """The auto-loop's ``_record_terminal`` must not move the phase.

    This drives the REAL loop: the orchestrator fails to start, the
    loop records its terminal, and the plan must stay exactly where the
    dead round left it.

    The control runs the same flow with a live token and shows the
    phase DOES move -- so "the phase did not change" is a refusal, not
    an artefact of the harness.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "zombie-loop-recorder"
    _seed_running_verification(db_path, plan_id, plans_root)
    plan_dir = plans_root / plan_id
    project_dir = plans_root / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _declare_dead(plan_dir, DEAD_TOKEN)

    monkeypatch.setattr(srv, "PlanState", _FakePlanState)
    monkeypatch.setattr(srv, "create_coding_tool", lambda *a, **k: None)

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the orchestrator could not start")

    monkeypatch.setattr(srv, "VerificationOrchestrator", _explode)

    vl._run_auto_verification_loop_inner(
        plan_id, plan_dir, project_dir, max_rounds=1, round_token=DEAD_TOKEN,
    )

    assert _phase(db_path, plan_id) == "verification_running", (
        "the dead round's own recorder moved the plan's phase — the plan "
        "is terminal for a round that was already declared dead"
    )
    assert _verification_status(db_path, plan_id) == "running", (
        "the dead round's own recorder wrote a verdict into "
        "plan_verification"
    )


def test_record_terminal_still_moves_the_phase_for_a_live_round(
    hermetic_server, monkeypatch,
):
    """The control for the test above: a live round's recorder works."""
    server, plans_root, db_path = hermetic_server
    plan_id = "zombie-loop-recorder-live"
    _seed_running_verification(db_path, plan_id, plans_root)
    plan_dir = plans_root / plan_id
    project_dir = plans_root / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(srv, "PlanState", _FakePlanState)
    monkeypatch.setattr(srv, "create_coding_tool", lambda *a, **k: None)

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the orchestrator could not start")

    monkeypatch.setattr(srv, "VerificationOrchestrator", _explode)

    vl._run_auto_verification_loop_inner(
        plan_id, plan_dir, project_dir, max_rounds=1, round_token=LIVE_TOKEN,
    )

    assert _phase(db_path, plan_id) == "failed", (
        "the live round's recorder did not move the plan's phase — the "
        "refusal must not have swallowed the normal path"
    )
    assert _verification_status(db_path, plan_id) == "failed"


def test_the_recorder_captures_its_own_round_token(
    hermetic_server, monkeypatch,
):
    """The recorder must use the token it was BUILT with.

    A newer round overwrites ``_verification_state[plan_id]
    ["round_token"]``.  A recorder that read the state at write time
    would name the LIVE round and its verdict would land as if the live
    round had written it -- the dead round would win by accident.

    So: build the recorder for a dead round, let a newer round take
    over the state, and the recorder must still refuse.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "zombie-captured-token"
    _seed_running_verification(db_path, plan_id, plans_root)
    plan_dir = plans_root / plan_id
    project_dir = plans_root / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _declare_dead(plan_dir, DEAD_TOKEN)

    # The repair callback is the loop's second recorder.  Its
    # ``_record_terminal`` is reached by the branch that stops the
    # chain when the repair batch never drained.
    srv._execution_state[plan_id] = {
        "_source": "repair_execution",
        "stop_reason": "repair_non_zero_exit",
    }
    monkeypatch.setattr(
        vl, "_get_pending_repair_tasks",
        lambda *a, **k: [{"task_id": "RP-1"}],
    )

    recorder = vl._make_repair_on_complete(
        plan_id, project_dir, round_token=DEAD_TOKEN,
    )

    # A NEWER round takes over the in-memory state.
    srv._verification_state[plan_id] = {
        "plan_id": plan_id,
        "verification_status": "running",
        "round_token": LIVE_TOKEN,
    }

    recorder(0)

    assert _phase(db_path, plan_id) == "verification_running", (
        "the dead round's recorder was refused for the wrong reason — it "
        "read the LIVE round's token out of the state instead of the "
        "token it was built with"
    )
    assert _verification_status(db_path, plan_id) == "running", (
        "the dead round's recorder wrote a verdict into plan_verification "
        "after a newer round had taken over the state"
    )


# ---------------------------------------------------------------------------
# 3. No second ``plan_closed`` event
# ---------------------------------------------------------------------------


def test_no_second_plan_closed_event_after_invalidation(
    hermetic_server, plan_closed_events,
):
    """The card is pushed once, not twice.

    ``complete_round`` fires ``KIND_PLAN_CLOSED`` on the state bus, and
    the notifier pushes the final card on it.  A dead round waking up
    and writing its terminal would fire a SECOND one -- a second card
    for a plan that was already closed, carrying a verdict nobody is
    waiting for.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "zombie-no-second-card"
    _seed_running_verification(db_path, plan_id, plans_root)
    plan_dir = plans_root / plan_id

    # The plan's round ends normally: one closed event.
    server._persist_verification_terminal(
        plan_id, "failed", "repair_exhausted", round_token=LIVE_TOKEN,
    )
    assert len(plan_closed_events.for_plan(plan_id)) == 1, (
        "precondition: the plan's own terminal write did not fire "
        "exactly one plan_closed event — the test below would prove "
        "nothing"
    )

    # The round is declared dead, and wakes up to write its terminal.
    _declare_dead(plan_dir, LIVE_TOKEN)
    server._persist_verification_terminal(
        plan_id, "failed", "repair_exhausted", round_token=LIVE_TOKEN,
    )

    assert len(plan_closed_events.for_plan(plan_id)) == 1, (
        "a round that was declared dead fired a SECOND plan_closed event "
        "— the operator gets a second card for a plan that was already "
        "closed"
    )


# ---------------------------------------------------------------------------
# 4. The repair-generation failure recorder
# ---------------------------------------------------------------------------


def test_the_generation_failure_of_a_dead_round_is_not_recorded(
    hermetic_server,
):
    """A dead round's repair-generation failure records nothing.

    The generation failure is written to ``plan_verification`` for
    visibility — and that write goes through ``complete_round``, which
    announces the plan's end.  A round that was declared dead must not
    do that either: the plan may already have a live round writing to
    the same row.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "zombie-generation-failure"
    _seed_running_verification(db_path, plan_id, plans_root)
    plan_dir = plans_root / plan_id
    _declare_dead(plan_dir, DEAD_TOKEN)

    vl._repair_generation_failed(
        plan_id, "the repair-task LLM call failed", round_token=DEAD_TOKEN,
    )

    assert _verification_status(db_path, plan_id) == "running", (
        "a round that was declared dead recorded its repair-generation "
        "failure on plan_verification — the plan now reports a verdict "
        "for a round it has already written off"
    )
    assert _phase(db_path, plan_id) == "verification_running"


def test_the_generation_failure_of_a_live_round_is_still_recorded(
    hermetic_server,
):
    """The anti-vacuity control for the test above.

    A live round's generation failure still lands — so "nothing was
    written" above means a refusal, not a broken recorder.
    """
    server, plans_root, db_path = hermetic_server
    plan_id = "zombie-generation-failure-live"
    _seed_running_verification(db_path, plan_id, plans_root)

    vl._repair_generation_failed(
        plan_id, "the repair-task LLM call failed", round_token=LIVE_TOKEN,
    )

    assert _verification_status(db_path, plan_id) == "failed", (
        "a live round's repair-generation failure was not recorded — the "
        "refusal is supposed to be about DEAD rounds only"
    )
    # The generation failure is a mid-loop record: it never ends the
    # chain, so the routing row stays where it was.
    assert _phase(db_path, plan_id) == "verification_running"


def test_the_loop_binds_its_own_round_token_into_the_failure_recorder():
    """The auto-loop's failure recorder carries THIS round's token.

    The module-level recorder is called BY NAME — the plan and the error
    and nothing else — so the round's token has to be bound by the loop
    that owns the round.  Reading it out of the in-memory state at write
    time would name a newer round instead, which is the mistake the
    brief forbids.

    This is a source-level pin for the same reason the existing pins in
    ``test_repair_generation_failure_handling.py`` are source-level:
    reaching that branch needs the whole orchestrator harness, and the
    harness would be testing more fixture than production.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(vl._run_auto_verification_loop_inner))
    recorder = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_repair_generation_failed"
    )
    forwarded = {
        keyword.arg
        for call in ast.walk(recorder)
        if isinstance(call, ast.Call)
        for keyword in call.keywords
    }
    assert "round_token" in forwarded, (
        "the auto-loop's repair-generation recorder does not forward the "
        "round's token — a dead round's failure would be recorded under "
        "whatever round the state happens to name"
    )

