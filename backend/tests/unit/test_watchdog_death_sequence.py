"""The watchdog's death sequence: cleanup → force release → invalidate (2026-11-01).

What this file pins
-------------------
Judging a verification round dead is not the same as the round being
dead.  The watchdog could see a round whose log went silent, stamp
``failed`` over the plan — and leave the round running, holding the
plan's workflow lease, spending the plan's money.  On 2026-10-31 that
is what kept an LLM call alive for 1h46m after the plan that made it
had already been written off; the plan said ``failed`` and the round
was still running, side by side, for an hour and three quarters.

So the judgement now comes with three acts, and they happen in this
order:

  1. **cleanup** — the round's own processes are killed, scoped to the
     round by its ``round_token`` (the rounds of one plan share one
     plan directory, so "the plan" is far too coarse a scope);
  2. **force release** — the plan's workflow lease is taken away, and
     only when the lease really carries THIS round's token;
  3. **invalidate** — the durable mark that tells the round, when it
     wakes up, that it has been written off.

and only then is the terminal state written.

The criteria this file pins, one test each:

* the three acts happen in that order, and the terminal write comes
  last;
* the force release is asked for the lease with the token the lease
  file actually carries;
* a lease carrying ANOTHER round's token is left exactly as it is;
* the dead round is marked invalid afterwards;
* a new round can take the plan's lease once the dead one is gone;
* EVERY stop reason cleans up first — not only the hard-cap branch;
* the watchdog's own terminal write is not blocked by the
  invalidation it has just written (the watchdog names no round);
* a cleanup that fails does not stop the terminal state.

Hermeticity
------------
Everything happens inside ``tmp_path``: the plan directory, the lease
file, the zombie-round marker and the state database are all inside the
test's own tree.  No process is spawned — the cleanup is observed, not
performed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server as server_mod  # noqa: E402
import verification_loop as vl  # noqa: E402
import zombie_rounds  # noqa: E402


#: The plan the watchdog is watching.
PLAN_ID = "20261101-death-sequence"

#: The token of the round the watchdog is judging dead.
DEAD_TOKEN = "verification_loop-1-ab"

#: The token of a round that took the plan over after the dead one.
LIVE_TOKEN = "verification_loop-2-cd"

#: The token of the round that comes after the death.
NEXT_TOKEN = "verification_loop-3-ef"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lease_path(plan_dir: Path) -> Path:
    """Where the workflow-level lease lives inside the plan directory."""
    return plan_dir / vl._VERIFICATION_WORKFLOW_LEASE_FILENAME


def _write_lease(plan_dir: Path, round_token: str) -> Path:
    """Put a workflow lease on disk, held by ``round_token``."""
    path = _lease_path(plan_dir)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(
        json.dumps({
            "round_token": round_token,
            "run_id": plan_dir.name,
            "pid": os.getpid(),
            "started_at": now,
            "heartbeat_at": now,
        }),
        encoding="utf-8",
    )
    return path


def _dead_thread() -> threading.Thread:
    """The orchestrator's own handle after its round died."""
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    assert not thread.is_alive()
    return thread


def _running_state(thread: Any) -> Dict[str, Any]:
    """A verification state that says "a round is running"."""
    return {
        "verification_status": "running",
        "verification_round": 1,
        "round_token": DEAD_TOKEN,
        "thread": thread,
        "started_at": "2026-11-01T03:00:00",
        # Well past the startup grace period, so the staleness ladder is
        # allowed to fire at all.
        "started_at_ts": time.time() - 7200.0,
    }


def _write_stale_round_log(plan_dir: Path, age: float) -> Path:
    """A round log that stopped being written ``age`` seconds ago."""
    logs = plan_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / "verification_1_20261101_030000.log"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "verification_point_id": "VP-010",
            "event_type": "vp_complete",
            "timestamp": "2026-11-01T03:05:00",
        }, ensure_ascii=False) + "\n")
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def _seed_running_verification(db_path: Path, plan_id: str) -> None:
    """Seed ``plan_routing`` + ``plan_verification`` mid-verification."""
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
        VerificationRepository(conn).init_round(plan_id, round_n=1, max_rounds=3)
    finally:
        conn.close()


def _phase(db_path: Path, plan_id: str) -> Any:
    """The plan's routing phase, as the state machine sees it."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


@pytest.fixture
def death_env(tmp_path, monkeypatch):
    """A plan the watchdog is about to judge dead.

    The plan directory, the state database and the lease all live inside
    the test's own tree.  The HITL sweep is stubbed out: it is
    orthogonal to the death sequence, and it would otherwise reach for
    the state database of the machine the test runs on.
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
    server_mod._verification_state[PLAN_ID] = _running_state(_dead_thread())

    yield {
        "plan_dir": plan_dir,
        "db_path": db_path,
        "plans_root": plans_root,
    }

    server_mod._verification_state.pop(PLAN_ID, None)
    server_mod._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN_ID, None)


def _run_watchdog() -> None:
    """Drive the production watchdog entry point."""
    server_mod._lazy_check_verification(PLAN_ID)


# ---------------------------------------------------------------------------
# 1. The order — cleanup, then the lease, then the mark, then the verdict
# ---------------------------------------------------------------------------


def test_watchdog_runs_cleanup_then_force_release_then_invalidate(death_env):
    """The three acts, in that order, and the terminal write last.

    The order is the contract.  The cleanup kills what the round left
    running; the force release hands the plan back; the invalidation
    writes the durable mark that stops the round from taking the plan
    again.  A verdict written before the cleanup would leave the plan
    saying ``failed`` while its round is still running — the very split
    this sequence exists to close.
    """
    order: List[str] = []
    _write_lease(death_env["plan_dir"], DEAD_TOKEN)

    def _cleanup(*args, **kwargs):
        order.append("cleanup")

    def _release(*args, **kwargs):
        order.append("force_release")
        return True

    def _invalidate(*args, **kwargs):
        order.append("invalidate")

    def _persist(*args, **kwargs):
        order.append("persist")

    with patch.object(
        server_mod, "_cleanup_dead_verification_processes", side_effect=_cleanup,
    ), patch.object(
        vl, "force_release_verification_workflow_lease", side_effect=_release,
    ), patch.object(
        vl.zombie_rounds, "invalidate", side_effect=_invalidate,
    ), patch.object(
        server_mod, "_persist_verification_terminal", side_effect=_persist,
    ):
        _run_watchdog()

    assert order == ["cleanup", "force_release", "invalidate", "persist"], (
        f"the death sequence ran in the wrong order: {order!r}"
    )


# ---------------------------------------------------------------------------
# 2. The lease — this round's lease, and only this round's lease
# ---------------------------------------------------------------------------


def test_watchdog_force_releases_with_the_holder_token(death_env):
    """The lease is released, and it is released with the token the lease
    file carries.

    The token is the gate.  The watchdog reads the holder's own token
    off the lease file and asks for exactly that lease — so the release
    is aimed at the lease that is really there, and the refusal path
    inside the force release is never reached by accident.
    """
    path = _write_lease(death_env["plan_dir"], DEAD_TOKEN)
    asked: List[Dict[str, Any]] = []
    real = vl.force_release_verification_workflow_lease

    def _spy(*args, **kwargs):
        asked.append(kwargs)
        return real(*args, **kwargs)

    with patch.object(server_mod, "_cleanup_dead_verification_processes"), \
         patch.object(
             vl, "force_release_verification_workflow_lease", _spy,
         ):
        _run_watchdog()

    assert asked, "the watchdog must ask for the lease to be released"
    assert asked[0].get("expected_token") == DEAD_TOKEN, (
        f"the release must name the holder's own token: {asked[0]!r}"
    )
    assert asked[0].get("actor") == "verification_watchdog", (
        f"the release must say WHO is taking the lease: {asked[0]!r}"
    )
    assert asked[0].get("reason") == "verification_thread_died_unexpectedly", (
        f"the release must say WHY: {asked[0]!r}"
    )
    assert not path.exists(), (
        "the dead round's lease must be gone — the next round of this "
        "plan has to be able to take it"
    )


def test_watchdog_does_not_force_release_a_foreign_token(death_env):
    """A lease that belongs to ANOTHER round is left exactly as it is.

    Somebody else's lease is not this watchdog's to take: a round that
    took the plan over while this one was being judged dead is the LIVE
    round now, and displacing it would be a second, worse split-brain.
    """
    path = _write_lease(death_env["plan_dir"], LIVE_TOKEN)

    with patch.object(server_mod, "_cleanup_dead_verification_processes"), \
         patch.object(
             vl, "force_release_verification_workflow_lease",
         ) as release:
        _run_watchdog()

    assert path.exists(), "the other round's lease must be left on disk"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["round_token"] == LIVE_TOKEN, (
        f"the other round's lease must be untouched: {record!r}"
    )
    for call in release.call_args_list:
        assert call.kwargs.get("expected_token") != LIVE_TOKEN, (
            f"the foreign lease must never be the one asked for: {call!r}"
        )


def test_a_missing_lease_skips_the_force_release(death_env):
    """No lease means nothing to release — and the plan still learns.

    A plan whose round held no lease (or whose lease file was already
    gone) is not blocked by the absence: the cleanup and the mark still
    happen, and the terminal state is still written.
    """
    with patch.object(server_mod, "_cleanup_dead_verification_processes"), \
         patch.object(
             vl, "force_release_verification_workflow_lease",
         ) as release:
        _run_watchdog()

    release.assert_not_called()
    assert zombie_rounds.is_invalidated(death_env["plan_dir"], DEAD_TOKEN), (
        "the round must still be marked dead when there is no lease"
    )
    assert _phase(death_env["db_path"], PLAN_ID) == "failed", (
        "the plan must still learn that its round died"
    )


# ---------------------------------------------------------------------------
# 3. The mark — the dead round is written off
# ---------------------------------------------------------------------------


def test_dead_round_is_invalidated(death_env):
    """The round is marked dead, with the reason and the actor.

    The mark is what the round consults when it wakes up: it may not
    take the plan's lease again, and it may not write a terminal state
    for a plan that has moved past it.  The reason and the actor are
    what an operator reads afterwards to answer "which round died, and
    on what grounds".
    """
    with patch.object(server_mod, "_cleanup_dead_verification_processes"), \
         patch.object(
             vl, "force_release_verification_workflow_lease", return_value=True,
         ):
        _run_watchdog()

    assert zombie_rounds.is_invalidated(
        death_env["plan_dir"], DEAD_TOKEN,
    ), "the dead round must be marked dead"

    mark = zombie_rounds.invalidated_round(death_env["plan_dir"])
    assert mark is not None, "the mark must be readable"
    assert mark["round_token"] == DEAD_TOKEN
    assert mark["reason"] == "verification_thread_died_unexpectedly", (
        f"the mark must say why the round died: {mark!r}"
    )
    assert mark["actor"] == "verification_watchdog", (
        f"the mark must say who wrote it off: {mark!r}"
    )


def test_the_dead_round_cannot_take_the_plan_back(death_env):
    """The round that was written off does not come back for the lease.

    The mark is durable and it is checked BEFORE the lock file is
    created, so the dead round is refused on its own identity.
    """
    with patch.object(server_mod, "_cleanup_dead_verification_processes"):
        _run_watchdog()

    lease, conflict = vl._acquire_verification_workflow_lease(
        death_env["plan_dir"], round_token=DEAD_TOKEN, run_id=PLAN_ID,
    )
    try:
        assert lease is None, f"the dead round took the plan back: {lease!r}"
        assert conflict is not None, "the refusal must be reported"
        assert conflict["reason"] == "round_invalidated", (
            f"the refusal must name the reason: {conflict!r}"
        )
    finally:
        if lease is not None:
            lease.release()


def test_a_new_start_is_accepted_after_the_death_sequence(death_env):
    """After the death, the plan is free again.

    The lease is what keeps two rounds of one plan from running at the
    same time, and it is released by the holder's own ``release()`` —
    which a killed round never runs.  So the death sequence has to take
    it away, or the next round of the plan is refused with
    ``round_conflict`` for as long as the file is there.
    """
    path = _write_lease(death_env["plan_dir"], DEAD_TOKEN)

    with patch.object(server_mod, "_cleanup_dead_verification_processes"):
        _run_watchdog()

    assert not path.exists(), (
        "the dead round's lease must be gone before the next round asks"
    )

    lease, conflict = vl._acquire_verification_workflow_lease(
        death_env["plan_dir"], round_token=NEXT_TOKEN, run_id=PLAN_ID,
    )
    try:
        assert conflict is None, (
            f"the next round must not be refused: {conflict!r}"
        )
        assert lease is not None, "the next round must be able to take the plan"
        assert _lease_path(death_env["plan_dir"]).exists()
    finally:
        if lease is not None:
            lease.release()


# ---------------------------------------------------------------------------
# 4. Every stop reason cleans up first
# ---------------------------------------------------------------------------


def test_a_stale_log_cleans_up_before_the_terminal_state(death_env):
    """The production incident's shape: the round log went silent.

    The cleanup used to run only on the hard-cap branch, so a round
    that was written off for a stale RESULTS file, or for a dead thread
    that was never cleaned, could keep running after its verdict.  Every
    stop reason now runs the cleanup first, aimed at the round that is
    being written off.
    """
    _write_stale_round_log(death_env["plan_dir"], age=3700.0)
    server_mod._verification_state[PLAN_ID] = _running_state(
        threading.current_thread(),
    )

    with patch.object(
        server_mod, "_cleanup_dead_verification_processes",
    ) as cleanup, patch.object(
        vl, "force_release_verification_workflow_lease", return_value=True,
    ):
        _run_watchdog()

    cleanup.assert_called_once(), (
        "a stale round log must clean the round up before the verdict"
    )
    assert cleanup.call_args.kwargs.get("round_token") == DEAD_TOKEN, (
        f"the cleanup must be aimed at the round: {cleanup.call_args!r}"
    )
    assert _phase(death_env["db_path"], PLAN_ID) == "failed", (
        "the stale round must still be terminalised"
    )


# ---------------------------------------------------------------------------
# 5. The verdict — the watchdog names no round, and a failure is not a verdict
# ---------------------------------------------------------------------------


def test_the_watchdog_verdict_is_not_blocked_by_its_own_invalidation(
    death_env,
):
    """The watchdog's own write names no round, so it is not refused.

    The terminal write is gated on the round that is writing it: a round
    that was written off may not close the plan.  The watchdog is the
    thing that DECLARES the death, so it writes with no round named —
    otherwise the mark it has just written would refuse its own verdict
    and the plan would never learn that its round died.
    """
    with patch.object(server_mod, "_cleanup_dead_verification_processes"), \
         patch.object(
             vl, "force_release_verification_workflow_lease", return_value=True,
         ):
        _run_watchdog()

    assert zombie_rounds.is_invalidated(
        death_env["plan_dir"], DEAD_TOKEN,
    ), "the round must be marked dead"
    assert _phase(death_env["db_path"], PLAN_ID) == "failed", (
        "the watchdog's verdict must land — the plan has to learn that "
        "its round died"
    )


def test_a_failing_cleanup_still_writes_the_terminal_state(death_env):
    """A kill that fails is not a reason to leave the plan in the dark.

    The cleanup, the release and the mark are all best-effort: whatever
    goes wrong, the plan is told that its round died.
    """
    with patch.object(
        server_mod, "_cleanup_dead_verification_processes",
        side_effect=RuntimeError("the kill failed"),
    ), patch.object(
        vl, "force_release_verification_workflow_lease", return_value=True,
    ):
        _run_watchdog()

    assert _phase(death_env["db_path"], PLAN_ID) == "failed", (
        "a cleanup that raised must not stop the terminal state"
    )
