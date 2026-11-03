"""A round that was declared dead must not take the plan's lease.

Background
----------
The plan's workflow-level lease (``.verification_round.lock`` inside the
plan directory) is what keeps two verification rounds of one plan from
running at the same time.  It is taken with ``O_CREAT | O_EXCL`` --
the creation itself is the atomic step -- so of two rounds racing for
the same plan exactly one wins.

The other half of the story is the round that was **declared dead**.
The watchdog can write a round off (its log went silent past the hard
ceiling, its VP subprocesses were killed) while the round itself is
still running: the LLM call returns, the code carries on, and the round
comes back for another turn.  A round that has been written off must
not come back and take the plan -- not write its terminal state (that
is the terminal-write gate), and not take the lease either.

The durable mark is the round's own ``round_token`` (see
``backend/zombie_rounds.py``): a round that was declared dead is
recorded in the plan directory, and the code that might wake up asks
``is_invalidated(plan_dir, round_token)`` before it acts.

This file pins the LEASE side of that mark.

Contract pinned here
--------------------
* ``_acquire_verification_workflow_lease`` refuses a round whose token
  was declared dead: no lease, and a conflict whose ``reason`` is
  ``"round_invalidated"`` and which names the token.
* The refusal happens BEFORE the ``O_CREAT | O_EXCL``: the lock file is
  not created, not created-and-deleted.  A create-then-delete would
  leave a window in which another round saw a live holder and was
  refused for no reason -- and would take away a live holder's lease.
* The refusal is written down: the plan's own execution log carries a
  ``verification_lease_acquire_refused`` event naming the token and
  the reason.
* A round whose token was NOT declared dead is unaffected: it takes the
  lease exactly as before.
* A marker file that cannot be read means "not known to be dead", not
  "the plan is stuck" -- the same degradation :mod:`zombie_rounds`
  promises.  A damaged bookkeeping file must never be the reason a
  live round cannot verify a plan.
* The stale-lease rule is NOT touched: a lease whose holder is alive is
  still not stale, and a refused (dead) round does not displace it.

Hermeticity
------------
Everything happens inside ``tmp_path``: the plan directory, the lock
file, the zombie marker and the execution log are all inside the test's
own tree.  No process is spawned.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server``
# imports back, so importing the inner one first trips the cycle.
import server  # noqa: E402,F401 — seeds sys.modules['server'] before the import
import verification_loop as vl  # noqa: E402
import zombie_rounds  # noqa: E402


#: The token of the round that was declared dead.
DEAD_TOKEN = "verification_loop-1-ab"

#: The token of a round that is still running.
LIVE_TOKEN = "verification_loop-2-cd"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_dir(
    tmp_path: Path, plan_id: str = "20261101-zombie-lease",
) -> Path:
    """The plan's directory -- the lease, the marker and the log live in it."""
    path = tmp_path / "plans" / plan_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _lease_path(plan_dir: Path) -> Path:
    """The workflow lease's path inside the plan directory."""
    return plan_dir / vl._VERIFICATION_WORKFLOW_LEASE_FILENAME


def _declare_dead(plan_dir: Path, round_token: str) -> None:
    """Mark ``round_token`` as declared dead for this plan."""
    zombie_rounds.invalidate(
        plan_dir, round_token,
        reason="verification_log_stale", actor="verification_watchdog",
    )


def _plan_events(tmp_path: Path, plan_id: str) -> List[Dict[str, Any]]:
    """The structured events the plan's execution log holds."""
    path = tmp_path / "plans" / plan_id / "execution.log"
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


# ---------------------------------------------------------------------------
# 1. The dead round does not get the lease
# ---------------------------------------------------------------------------


def test_invalidated_round_cannot_acquire_the_lease(tmp_path: Path) -> None:
    """A round that was declared dead must not take the plan's lease.

    The round is refused by its own identity: the token it asks with is
    the token the watchdog wrote off.
    """
    plan_dir = _plan_dir(tmp_path)
    _declare_dead(plan_dir, DEAD_TOKEN)

    lease, conflict = vl._acquire_verification_workflow_lease(
        plan_dir, round_token=DEAD_TOKEN, run_id=plan_dir.name,
    )

    assert lease is None, (
        f"a round that was declared dead took the plan's lease: {lease!r}"
    )
    assert conflict is not None, (
        "the refusal must be reported: a round that gets no lease and no "
        "conflict cannot tell 'refused' from 'the plan has no lease at all'"
    )
    assert conflict["reason"] == "round_invalidated", (
        f"the refusal must name the reason: {conflict!r}"
    )
    assert conflict["round_token"] == DEAD_TOKEN, (
        f"the refusal must name the round it refused: {conflict!r}"
    )
    assert "holder_pid" in conflict, (
        f"the refusal must carry the conflict shape the round reads: {conflict!r}"
    )


def test_no_lock_file_is_created_for_an_invalidated_round(tmp_path: Path) -> None:
    """The gate is BEFORE the ``O_CREAT | O_EXCL``, not after it.

    A create-then-delete would leave a window in which the file existed
    -- long enough for another round to see a holder that was never
    there, and long enough for the file to be seen as a live lease.
    """
    plan_dir = _plan_dir(tmp_path)
    _declare_dead(plan_dir, DEAD_TOKEN)

    lease, conflict = vl._acquire_verification_workflow_lease(
        plan_dir, round_token=DEAD_TOKEN, run_id=plan_dir.name,
    )

    assert lease is None, f"the dead round took the lease: {lease!r}"
    assert conflict is not None, f"the refusal must be reported: {conflict!r}"
    assert not _lease_path(plan_dir).exists(), (
        f"the lock file {_lease_path(plan_dir)} was created for a round that "
        f"was declared dead"
    )


def test_a_fresh_token_still_acquires_the_lease(tmp_path: Path) -> None:
    """The gate is about the DEAD round, not about leases in general.

    A round whose token was never declared dead takes the lease exactly
    as it did before -- and it takes it even while another round's mark
    is on file.
    """
    plan_dir = _plan_dir(tmp_path)
    _declare_dead(plan_dir, DEAD_TOKEN)

    lease, conflict = vl._acquire_verification_workflow_lease(
        plan_dir, round_token=LIVE_TOKEN, run_id=plan_dir.name,
    )
    try:
        assert conflict is None, (
            f"a live round must not be refused: {conflict!r}"
        )
        assert lease is not None, "a live round must get the lease"
        assert _lease_path(plan_dir).exists(), (
            "the live round's lease must be on disk"
        )
    finally:
        if lease is not None:
            lease.release()


# ---------------------------------------------------------------------------
# 2. The refusal is written down
# ---------------------------------------------------------------------------


def test_refusal_is_recorded_with_token_and_reason(tmp_path: Path) -> None:
    """The refusal lands in the plan's own execution log.

    "A round asked for the lease and did not get it" is a fact the
    operator needs after the fact: without it, a plan whose round went
    quiet looks exactly like a plan whose round is still working.
    """
    plan_dir = _plan_dir(tmp_path)
    _declare_dead(plan_dir, DEAD_TOKEN)

    vl._acquire_verification_workflow_lease(
        plan_dir, round_token=DEAD_TOKEN, run_id=plan_dir.name,
    )

    events = _plan_events(tmp_path, plan_dir.name)
    refusals = [
        e for e in events
        if e.get("event") == "verification_lease_acquire_refused"
    ]
    assert refusals, (
        "the refusal must be written to the plan's execution log; the log "
        f"holds {sorted({str(e.get('event')) for e in events})!r}"
    )
    data = refusals[0].get("data") or {}
    assert data.get("round_token") == DEAD_TOKEN, (
        f"the refusal must name the round: {data!r}"
    )
    assert data.get("reason") == "round_invalidated", (
        f"the refusal must name the reason: {data!r}"
    )


# ---------------------------------------------------------------------------
# 3. The gate must not cost the plan anything
# ---------------------------------------------------------------------------


def test_a_damaged_marker_file_does_not_refuse_the_lease(
    tmp_path: Path,
) -> None:
    """A marker that cannot be read means "not known to be dead".

    The mark is bookkeeping.  If it cannot be read, the plan must still
    be able to verify -- a damaged file must never be the reason a
    live round is locked out.
    """
    plan_dir = _plan_dir(tmp_path)
    marker = plan_dir / zombie_rounds.ZOMBIE_ROUNDS_FILENAME
    marker.write_text("{ this is not json", encoding="utf-8")

    lease, conflict = vl._acquire_verification_workflow_lease(
        plan_dir, round_token=DEAD_TOKEN, run_id=plan_dir.name,
    )
    try:
        assert conflict is None, (
            f"a damaged marker file must not refuse a round: {conflict!r}"
        )
        assert lease is not None, "a damaged marker file must not lock the plan out"
    finally:
        if lease is not None:
            lease.release()


def test_the_refusal_does_not_displace_a_live_holder(tmp_path: Path) -> None:
    """A refused (dead) round must not touch the live round's lease.

    The dead round is refused on its own identity, so it never gets as
    far as the file: the live holder's lease is still on disk, still
    carries the live holder's token, and is not stale (its pid is
    alive) -- the stale-lease rule is not touched by this gate.
    """
    plan_dir = _plan_dir(tmp_path)
    holder, _ = vl._acquire_verification_workflow_lease(
        plan_dir, round_token=LIVE_TOKEN, run_id=plan_dir.name,
    )
    try:
        _declare_dead(plan_dir, DEAD_TOKEN)

        lease, conflict = vl._acquire_verification_workflow_lease(
            plan_dir, round_token=DEAD_TOKEN, run_id=plan_dir.name,
        )

        assert lease is None, f"the dead round took the lease: {lease!r}"
        assert conflict is not None, f"the refusal must be reported: {conflict!r}"
        assert conflict["reason"] == "round_invalidated", (
            f"the refusal must name the reason: {conflict!r}"
        )
        assert _lease_path(plan_dir).exists(), (
            "the live holder's lease was removed by a round that was "
            "refused"
        )
        record = json.loads(
            _lease_path(plan_dir).read_text(encoding="utf-8"),
        )
        assert record["round_token"] == LIVE_TOKEN, (
            f"the live holder's lease was overwritten: {record!r}"
        )
        assert vl._workflow_lease_stale_reason(
            _lease_path(plan_dir), record,
        ) is None, (
            "a lease whose holder is alive is not stale, and this gate must "
            "not change that"
        )
    finally:
        holder.release()
