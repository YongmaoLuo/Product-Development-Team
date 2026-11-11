"""Divergent verification verdicts: the round has a verdict, the row says running.

What this file pins
-------------------
A verification round can reach its verdict and still leave the plan's own
row saying ``running``:

  * the auto-loop stamps the round's verdict into the in-memory
    ``_verification_state`` (``verification_status = "verification_failed"``)
    while the plan's ``plan_verification`` row keeps the ``running``
    value stamped at round start;
  * the watchdog's death mark writes its side-channel snapshot
    (``.verification_runtime.json``) and the process dies before the
    terminal write lands.

Either way the two disagree — and the disagreement is SILENT.  The
watchdog's ladder only inspects plans whose memory says ``running``, so
it never revisits the plan; the startup sweep only converges rows whose
``verification_status`` is ALREADY terminal, so it never converges this
one either.  The plan hangs at "verifying" for ever, with nothing in the
log to say why.

The reconciliation this file pins:

  * the watchdog converges a divergent memory verdict to ``failed`` —
    exactly ONE terminal write, carrying the reason the round recorded;
  * the startup sweep converges a divergent on-disk snapshot the same
    way, and drops the in-memory entry so ``/status`` cannot report
    "running" for a plan whose chain has ended;
  * a live round, a repair execution in flight, and a row that already
    carries a terminal verdict are all left exactly as they are;
  * the convergence writes the TERMINAL state and nothing else: no death
    sequence, no orphan kill, no zombie mark, no lease touch.  A round
    that reached a verdict owns none of those — they belong to a round
    that was judged DEAD, and this one was not.

Hermeticity
------------
Everything lives inside ``tmp_path``: the plan directory, the state
database and the runtime snapshot.  No process is spawned — the death
sequence is observed, never performed.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server as server_mod  # noqa: E402
import verification_loop as vl  # noqa: E402


#: The plan whose round and whose row disagree.
PLAN_ID = "20261111-divergent-verdict"

#: The reason the failed round recorded.
STOP_REASON = "same_failure_repeated_after_max_attempts"

#: The reason a round that recorded none gets.
FALLBACK_REASON = "verification_round_verdict_unrecorded"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_running_verification(db_path: Path, plan_id: str) -> None:
    """Seed ``plan_routing`` + ``plan_verification`` mid-verification.

    ``init_round`` inserts the verification row with
    ``verification_status = "running"`` — the value the divergence is
    measured against.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(str(db_path))
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "verification_running")
        VerificationRepository(conn).init_round(
            plan_id, round_n=1, max_rounds=3,
        )
    finally:
        conn.close()


def _set_db_status(db_path: Path, plan_id: str, status: str) -> None:
    """Force the plan's own verification row to ``status``."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE plan_verification SET verification_status = ? "
            "WHERE plan_id = ?",
            (status, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _db_status(db_path: Path, plan_id: str) -> Any:
    """The plan's own verification row, as the database sees it."""
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


def _dead_thread() -> threading.Thread:
    """The orchestrator's own handle after its round died."""
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    assert not thread.is_alive()
    return thread


def _write_runtime_snapshot(
    plan_dir: Path, status: str, stop_reason: str = STOP_REASON,
    round_n: Any = None,
) -> Path:
    """The side-channel snapshot a round leaves behind on disk."""
    payload: Dict[str, Any] = {
        "verification_status": status,
        "stop_reason": stop_reason,
    }
    if round_n is not None:
        payload["verification_round"] = round_n
    path = plan_dir / ".verification_runtime.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _set_db_round(db_path: Path, plan_id: str, round_n: int) -> None:
    """Force the plan's own verification row to ``round_n``."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE plan_verification SET round = ? WHERE plan_id = ?",
            (round_n, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _divergent_state(**overrides: Any) -> Dict[str, Any]:
    """An in-memory record whose round already reached a verdict."""
    state: Dict[str, Any] = {
        "verification_status": "verification_failed",
        "stop_reason": STOP_REASON,
        "thread": _dead_thread(),
        "verification_round": 2,
    }
    state.update(overrides)
    return state


@pytest.fixture
def divergent_env(tmp_path, monkeypatch):
    """A plan whose round and whose row disagree.

    The plan directory, the state database and the runtime snapshot all
    live inside the test's own tree.  The HITL sweep is stubbed out: it
    is orthogonal to the reconciliation, and it would otherwise reach
    for the state database of the machine the test runs on.
    """
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(server_mod, "PLANS_DIR", plans_root)
    monkeypatch.setattr(
        server_mod, "_state_db_path", lambda *a, **k: str(db_path),
    )
    plan_dir = plans_root / PLAN_ID
    plan_dir.mkdir()
    _seed_running_verification(db_path, PLAN_ID)

    monkeypatch.setattr(vl, "_expire_hitl_decisions", lambda now: 0)
    monkeypatch.setattr(
        vl, "_handle_hitl_strict_timeout", lambda plan_id, now: None,
    )
    server_mod._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN_ID, None)
    # The hand-off predicate reads the process-global execution map; a
    # leftover entry from another test would make this plan look like a
    # repair in flight.
    server_mod._execution_state.pop(PLAN_ID, None)
    server_mod._verification_state.pop(PLAN_ID, None)

    yield {
        "plan_dir": plan_dir,
        "db_path": db_path,
        "plans_root": plans_root,
    }

    server_mod._verification_state.pop(PLAN_ID, None)
    server_mod._execution_state.pop(PLAN_ID, None)
    server_mod._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN_ID, None)


# ---------------------------------------------------------------------------
# 1. The watchdog converges a divergent memory verdict
# ---------------------------------------------------------------------------


def test_divergent_failed_memory_is_persisted(divergent_env):
    """The round has a verdict; the row still says ``running``.

    The watchdog writes the verdict the round decided on — exactly once,
    with the reason the round recorded — and the memory settles on
    ``failed`` so the next tick sees a terminal plan rather than a
    divergent one.
    """
    server_mod._verification_state[PLAN_ID] = _divergent_state()

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        server_mod._lazy_check_verification(PLAN_ID)

    persist.assert_called_once_with(
        PLAN_ID, "failed", STOP_REASON, chain_ending=True,
    )
    assert server_mod._verification_state[PLAN_ID]["verification_status"] == (
        "failed"
    ), (
        "the plan's own memory must settle on the terminal verdict — a "
        "divergent memory is what kept the watchdog from ever looking at "
        "the plan again"
    )


def test_divergent_memory_without_a_reason_gets_a_named_one(divergent_env):
    """A round that recorded no reason still gets a named one.

    An empty reason would write an empty ``stop_reason`` into the row,
    and the operator reading the plan afterwards would have nothing to
    go on.  The placeholder says what the write was: the round's verdict
    was never recorded.
    """
    server_mod._verification_state[PLAN_ID] = _divergent_state(
        stop_reason="",
    )

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        server_mod._lazy_check_verification(PLAN_ID)

    persist.assert_called_once_with(
        PLAN_ID, "failed", FALLBACK_REASON, chain_ending=True,
    )


# ---------------------------------------------------------------------------
# 2. What is NOT a divergence
# ---------------------------------------------------------------------------


def test_non_divergent_states_are_not_reconciled(divergent_env):
    """A memory that carries no verdict is left exactly as it is.

    ``running`` is a live round (the detection ladder owns it),
    ``paused`` is a round waiting for a human, ``passed`` is a round
    that succeeded, ``loop_stopped`` is a round that gave up on its own
    terms.  None of them is a divergence, and none of them may be
    rewritten by the reconciliation.
    """
    for status in ("running", "paused", "passed", "loop_stopped"):
        server_mod._verification_state[PLAN_ID] = _divergent_state(
            verification_status=status,
            # A live thread: a dead one would be the ladder's business,
            # not the reconciliation's.
            thread=threading.current_thread(),
        )

        with patch.object(
            server_mod, "_persist_verification_terminal",
        ) as persist:
            server_mod._lazy_check_verification(PLAN_ID)

        persist.assert_not_called(), (
            f"a {status!r} plan is not a divergence and must not be rewritten"
        )


def test_live_thread_is_not_reconciled(divergent_env):
    """A divergent memory with a LIVE thread is not a divergence.

    The round is still running: it may be between two writes, and it
    owns the plan until it says otherwise.  Converging it would close
    the plan under a round that is still working.
    """
    server_mod._verification_state[PLAN_ID] = _divergent_state(
        thread=threading.current_thread(),
    )

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        server_mod._lazy_check_verification(PLAN_ID)

    persist.assert_not_called(), (
        "a round that is still alive owns the plan — the reconciliation "
        "must not close it"
    )


def test_in_flight_repair_is_not_converged(divergent_env):
    """A repair execution in flight owns the plan right now.

    ``_plan_execution_in_flight`` is the ONE answer in the process to
    "is a background execution outstanding for this plan" (the hand-off
    criterion the watchdog's death ladder already uses).  While it is
    true the plan is being worked on, and a terminal verdict would close
    it under the repair's feet.
    """
    server_mod._execution_state[PLAN_ID] = {
        "status": "running",
        "ended_at": None,
    }
    server_mod._verification_state[PLAN_ID] = _divergent_state()

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        server_mod._lazy_check_verification(PLAN_ID)

    persist.assert_not_called(), (
        "a plan with a repair in flight must not be converged"
    )


def test_db_already_terminal_is_not_rewritten(divergent_env):
    """A row that already carries a terminal verdict is not written again.

    Rewriting it would fire a SECOND ``plan_closed`` for a plan that was
    already closed, and push a second card for it.
    """
    _set_db_status(divergent_env["db_path"], PLAN_ID, "failed")
    server_mod._verification_state[PLAN_ID] = _divergent_state()

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        server_mod._lazy_check_verification(PLAN_ID)

    persist.assert_not_called(), (
        "a plan whose row is already terminal must not be written again"
    )


# ---------------------------------------------------------------------------
# 3. The convergence writes the verdict — and nothing else
# ---------------------------------------------------------------------------


def test_divergence_writes_no_death_sequence(divergent_env):
    """No death sequence: the round was not judged dead, it finished.

    The death sequence kills the round's processes, takes its lease and
    marks it invalid — the acts of judging a round DEAD.  A round that
    reached a verdict needs none of them: it owns its processes, its
    lease and its round, and the reconciliation is a bookkeeping write.
    """
    server_mod._verification_state[PLAN_ID] = _divergent_state()

    with patch.object(
        server_mod, "_persist_verification_terminal",
    ) as persist, patch.object(
        server_mod, "_mark_verification_failed_dead",
    ) as mark, patch.object(
        server_mod, "_kill_orphaned_vp_processes",
    ) as kill:
        server_mod._lazy_check_verification(PLAN_ID)

    persist.assert_called_once()
    mark.assert_not_called(), (
        "a round that reached a verdict must not be marked dead"
    )
    kill.assert_not_called(), (
        "a round that reached a verdict must not have its processes killed"
    )


# ---------------------------------------------------------------------------
# 4. The startup sweep converges the same shape after a restart
# ---------------------------------------------------------------------------


def test_startup_reconcile_converges_divergent_runtime(divergent_env):
    """A restart must not leave a diverged plan hanging.

    The plan's row says ``running``; the snapshot the round wrote on
    disk says ``verification_failed``.  The restart took the round's
    memory with it, so nothing holds the plan any more — the startup
    sweep converges it: one terminal write, and the in-memory entry is
    dropped so ``/status`` cannot report "running" for a plan whose
    chain has ended.
    """
    _write_runtime_snapshot(divergent_env["plan_dir"], "verification_failed")
    server_mod._verification_state[PLAN_ID] = {
        "verification_status": "running",
        "orchestrator": None,
        "thread": None,
    }

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        server_mod._reconcile_orphaned_verification_stages()

    persist.assert_called_once_with(
        PLAN_ID, "failed", STOP_REASON, chain_ending=True,
    )
    assert PLAN_ID not in server_mod._verification_state, (
        "the reconciled plan must be evicted from the in-memory "
        "verification state so /status cannot report 'running'"
    )


def test_startup_reconcile_leaves_genuinely_running_alone(divergent_env):
    """A round that is really running is left to the watchdog.

    Two shapes, and both must survive the startup sweep untouched: the
    snapshot says the round is still running, and the snapshot carries a
    verdict while a repair execution is in flight (the repair owns the
    plan right now).
    """
    # (a) the snapshot says the round is still running.
    _write_runtime_snapshot(divergent_env["plan_dir"], "running")
    server_mod._verification_state[PLAN_ID] = {
        "verification_status": "running",
        "orchestrator": None,
        "thread": None,
    }

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        server_mod._reconcile_orphaned_verification_stages()

    persist.assert_not_called(), (
        "a round that is really running must be left to the watchdog"
    )
    assert PLAN_ID in server_mod._verification_state

    # (b) the snapshot carries a verdict, but a repair is in flight.
    _write_runtime_snapshot(divergent_env["plan_dir"], "verification_failed")
    server_mod._execution_state[PLAN_ID] = {
        "status": "running",
        "ended_at": None,
    }

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        server_mod._reconcile_orphaned_verification_stages()

    persist.assert_not_called(), (
        "a plan with a repair in flight must not be converged at startup"
    )
    assert PLAN_ID in server_mod._verification_state


def test_startup_reconcile_without_a_snapshot_is_a_noop(divergent_env):
    """No snapshot at all → nothing to converge, nothing is written."""
    server_mod._verification_state[PLAN_ID] = {
        "verification_status": "running",
        "orchestrator": None,
        "thread": None,
    }

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        server_mod._reconcile_orphaned_verification_stages()

    persist.assert_not_called()
    assert PLAN_ID in server_mod._verification_state


def test_startup_reconcile_ignores_an_unparseable_snapshot(divergent_env):
    """A snapshot nobody can read is not a verdict.

    The side-channel file is written by a process that may have been
    killed mid-write, so a half-written file is a shape that really
    happens.  It must not crash the startup sweep, and it must not be
    read as a verdict — the plan keeps the behaviour it had.
    """
    (divergent_env["plan_dir"] / ".verification_runtime.json").write_text(
        '{"verification_status": "verification_fai', encoding="utf-8",
    )
    server_mod._verification_state[PLAN_ID] = {
        "verification_status": "running",
        "orchestrator": None,
        "thread": None,
    }

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        server_mod._reconcile_orphaned_verification_stages()

    persist.assert_not_called(), (
        "a snapshot that cannot be parsed must not close the plan"
    )
    assert PLAN_ID in server_mod._verification_state


def test_startup_reconcile_ignores_an_earlier_round_snapshot(divergent_env):
    """An OLD round's verdict is not a divergence.

    The side-channel snapshot is not cleared when a new round starts, so
    a plan that was restarted still carries round 1's verdict on disk
    next to a row that says "round 2 is running".  Closing the plan on
    that old verdict would fail a round that has only just begun.
    """
    _write_runtime_snapshot(
        divergent_env["plan_dir"], "verification_failed", round_n=1,
    )
    _set_db_round(divergent_env["db_path"], PLAN_ID, 2)
    server_mod._verification_state[PLAN_ID] = {
        "verification_status": "running",
        "orchestrator": None,
        "thread": None,
    }

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        server_mod._reconcile_orphaned_verification_stages()

    persist.assert_not_called(), (
        "a snapshot from a round the plan has already left must not close "
        "the plan"
    )
    assert PLAN_ID in server_mod._verification_state


# ---------------------------------------------------------------------------
# 5. The negative control
# ---------------------------------------------------------------------------


def test_removing_the_divergent_branch_stops_the_write(
    divergent_env, monkeypatch,
):
    """Without the divergent branch there is NO write — the defect.

    The latch in ``_lazy_check_verification`` is what routes a divergent
    memory to the reconciliation at all.  Empty the set of divergent
    statuses and the plan is left exactly as it was: the watchdog never
    looks at it again, and the plan hangs at "verifying" for ever.
    """
    monkeypatch.setattr(
        vl, "_VERIFICATION_DIVERGENT_MEMORY_STATUSES", frozenset(),
    )
    server_mod._verification_state[PLAN_ID] = _divergent_state()

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        server_mod._lazy_check_verification(PLAN_ID)

    persist.assert_not_called(), (
        "the negative control FAILED: the write happened even without the "
        "divergent branch — the latch is not what routes it"
    )


# ---------------------------------------------------------------------------
# 6. The reader the reconciliation stands on
# ---------------------------------------------------------------------------


def test_read_plan_verification_status_reports_the_row(divergent_env):
    """The reader answers with the row's own value — and nothing else.

    ``None`` means "no row, or the row could not be read"; the caller
    must then leave the plan alone rather than guess.
    """
    assert vl._read_plan_verification_status(PLAN_ID) == "running"

    _set_db_status(divergent_env["db_path"], PLAN_ID, "failed")
    assert vl._read_plan_verification_status(PLAN_ID) == "failed"

    assert vl._read_plan_verification_status("no-such-plan-at-all") is None


def test_divergent_statuses_are_the_two_spellings():
    """The divergent set is exactly the two spellings of a failed round.

    ``verification_failed`` is the orchestrator's own word for a failed
    round; ``failed`` is the word the watchdog's death mark writes.  A
    verdict the round already reached, in either spelling.
    """
    assert vl._VERIFICATION_DIVERGENT_MEMORY_STATUSES == frozenset(
        {"verification_failed", "failed"},
    )


# ---------------------------------------------------------------------------
# 7. The fourth gate: a plan that is legitimately continuing is left alone
# ---------------------------------------------------------------------------


def _set_db_phase(db_path: Path, plan_id: str, phase: str) -> None:
    """Force the plan's routing row to ``phase``.

    The raw column is written on purpose: the historical value
    ``verification_rerunning`` is no longer part of the phase
    vocabulary, so it can only be put on disk directly.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE plan_routing SET current_phase = ? WHERE plan_id = ?",
            (phase, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_repairing_phase_is_not_converged(divergent_env):
    """A plan parked at ``verification_repairing`` is not converged.

    ``verification_repairing`` is the user-gated pause BETWEEN rounds:
    the round finished, its repair tasks are waiting to be confirmed,
    and the plan is legitimately CONTINUING.  All three gates the
    reconciliation had are true for such a plan — the next round has
    not started, so there is no thread and no background execution, and
    the previous round's ``running`` row is exactly what is on disk.

    Converging it would write off a plan that is mid-way through its
    verification chain: the user would be asked to confirm a repair for
    a plan that has already been closed as failed.

    The phase is therefore the FOURTH gate, and it is read AFTER the
    other three — a candidate the first three already rejected costs no
    extra database read.
    """
    _set_db_phase(
        divergent_env["db_path"], PLAN_ID, "verification_repairing",
    )
    state = _divergent_state()

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        assert vl._divergence_gates_pass(PLAN_ID, state) is False, (
            "a plan parked at verification_repairing is legitimately "
            "continuing — the divergence gates must not pass"
        )
        assert vl._reconcile_divergent_verdict(PLAN_ID, state) is False

    assert persist.call_count == 0, (
        "a plan waiting for its repair to be confirmed must not be closed"
    )


def test_historical_rerunning_phase_is_not_converged(divergent_env):
    """The historical ``verification_rerunning`` value is excluded too.

    ``verification_rerunning`` left the phase vocabulary on 2026-10-22;
    a row written before that still carries it.  The READ path maps it
    onto the phase that replaced it — ``verification_repairing`` — so a
    comparison against the literal string ``verification_repairing``
    would miss it and close the plan.

    The gate therefore normalizes the phase BEFORE comparing, and this
    test holds the raw historical value on disk.
    """
    _set_db_phase(
        divergent_env["db_path"], PLAN_ID, "verification_rerunning",
    )
    state = _divergent_state()

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        assert vl._divergence_gates_pass(PLAN_ID, state) is False, (
            "the historical verification_rerunning value reads as "
            "verification_repairing — it must not converge either"
        )
        assert vl._reconcile_divergent_verdict(PLAN_ID, state) is False

    assert persist.call_count == 0, (
        "a plan carrying the historical verification_rerunning value must "
        "not be closed either"
    )


def test_running_phase_divergence_is_converged(divergent_env):
    """The fourth gate does not close the normal path.

    A plan whose phase is ``verification_running`` and whose round has
    already reached a verdict is the very shape the reconciliation
    exists for: the round decided, the row never heard.  It converges —
    exactly one terminal write.
    """
    _set_db_phase(
        divergent_env["db_path"], PLAN_ID, "verification_running",
    )
    state = _divergent_state()

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        assert vl._divergence_gates_pass(PLAN_ID, state) is True, (
            "a verification_running plan with a divergent verdict is the "
            "shape the reconciliation exists for"
        )
        assert vl._reconcile_divergent_verdict(PLAN_ID, state) is True

    assert persist.call_count == 1, (
        "the fourth gate must not close the normal path — a "
        "verification_running divergence must still be converged"
    )


def test_phase_read_failure_keeps_original_gates(
    divergent_env, monkeypatch, caplog,
):
    """A phase that cannot be read leaves the previous behaviour alone.

    The fourth gate is an EXCLUSION, so a phase that cannot be read is
    not evidence of a legitimate continuation: the plan is treated as
    "not continuing" and the three original gates decide, exactly as
    they did before this gate existed.  The failed read is logged with
    the plan id — a gate that silently stopped working is how this
    defect got here in the first place.
    """
    def _raise(plan_id):
        raise RuntimeError("the routing row could not be read")

    monkeypatch.setattr(vl, "_read_plan_current_phase", _raise)
    state = _divergent_state()

    with caplog.at_level("WARNING", logger="server"):
        with patch.object(
            server_mod, "_persist_verification_terminal",
        ) as persist:
            assert vl._divergence_gates_pass(PLAN_ID, state) is True, (
                "an unreadable phase must not change what the other three "
                "gates decide"
            )
            assert vl._reconcile_divergent_verdict(PLAN_ID, state) is True

    assert persist.call_count == 1, (
        "an unreadable phase must not change what the other three gates "
        "decide"
    )
    messages = [
        record.getMessage() for record in caplog.records
        if record.name == "server"
    ]
    assert any(
        PLAN_ID in message and "phase" in message for message in messages
    ), (
        "a phase read that fails must be logged with the plan id — "
        f"got {messages!r}"
    )


def test_phase_is_not_read_when_an_earlier_gate_fails(
    divergent_env, monkeypatch,
):
    """The fourth gate is LAST: an earlier rejection reads no phase.

    A candidate the first three gates already rejected must not cost an
    extra database read — and the ordering is what keeps the existing
    negative controls behaving exactly as they did before this gate
    existed.
    """
    reads = []

    def _counting_reader(plan_id):
        reads.append(plan_id)
        return "verification_repairing"

    monkeypatch.setattr(vl, "_read_plan_current_phase", _counting_reader)

    # (a) the round's thread is still alive → the first gate rejects.
    assert vl._divergence_gates_pass(
        PLAN_ID, _divergent_state(thread=threading.current_thread()),
    ) is False
    assert reads == [], (
        "the phase must not be read once an earlier gate has rejected the "
        f"candidate — got {reads!r}"
    )

    # (b) the row already carries a terminal verdict → the third gate
    # rejects.
    _set_db_status(divergent_env["db_path"], PLAN_ID, "failed")
    assert vl._divergence_gates_pass(PLAN_ID, _divergent_state()) is False
    assert reads == [], (
        "the phase must not be read once an earlier gate has rejected the "
        f"candidate — got {reads!r}"
    )
