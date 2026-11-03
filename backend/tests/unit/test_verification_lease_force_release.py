"""Force-releasing the verification workflow lease (2026-11-01).

The criterion this file pins
----------------------------
The workflow lease (``.verification_round.lock`` inside the plan
directory) is released by exactly one thing: the holder's own
``release()``, which only acts on a file that still carries ITS OWN
``round_token``.  So a round that was judged dead — killed, wedged, its
heartbeat thread gone — keeps the lease, and the next round of the same
plan is refused with ``round_conflict`` for as long as the file is
there.

The stale-lease rule does not help either.  A lease whose holder is
still ALIVE is never stale by pid, and this file deliberately does not
change that: a live holder is not to be displaced on a guess.  The
criterion this file pins is therefore a THIRD way out — an explicit,
auditable, token-gated force release:

  * the caller names the ``round_token`` it believes the lease carries;
    a lease carrying a DIFFERENT token is refused and left untouched;
  * the caller names WHO is releasing (``actor``) and WHY
    (``reason``), and both land in the plan's own execution log;
  * the caller's ``cleanup`` (killing the round's processes) runs
    BEFORE the file is removed, so the holder gets the chance to give
    the plan back by itself;
  * the release waits a bounded window for that, and if a DIFFERENT
    round has taken the plan over by then, that round's lease is left
    alone.

Hermeticity
------------
Everything happens inside ``tmp_path``: the lease file and the plan's
``execution.log`` are the only things written, and both live in the
test's own plan directory.  No process is spawned.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import server  # noqa: F401 — seeds sys.modules['server'] before the import
import verification_loop as vl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_dir(tmp_path: Path, plan_id: str = "20261101-force-release") -> Path:
    """The plan's directory — the lease and the log live inside it."""
    path = tmp_path / "plans" / plan_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _workflow_lease_path(plan_dir: Path) -> Path:
    """Where the workflow-level lease lives."""
    return plan_dir / vl._VERIFICATION_WORKFLOW_LEASE_FILENAME


def _write_lease(
    plan_dir: Path,
    *,
    round_token: str,
    pid: Optional[int] = None,
    heartbeat_at: Optional[str] = None,
) -> Path:
    """Put a workflow lease on disk, held by ``round_token``."""
    path = _workflow_lease_path(plan_dir)
    now = datetime.now().isoformat()
    path.write_text(
        json.dumps({
            "round_token": round_token,
            "run_id": plan_dir.name,
            "pid": os.getpid() if pid is None else int(pid),
            "started_at": now,
            "heartbeat_at": heartbeat_at or now,
        }),
        encoding="utf-8",
    )
    return path


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


def _events_named(tmp_path: Path, plan_id: str, name: str) -> List[Dict[str, Any]]:
    return [e for e in _plan_events(tmp_path, plan_id) if e.get("event") == name]


# ---------------------------------------------------------------------------
# 1. The token gate — a lease that is not the one we were asked to release
#    is left exactly as it was
# ---------------------------------------------------------------------------


def test_force_release_refuses_a_token_mismatch(tmp_path: Path) -> None:
    """A force release only ever touches the lease it names.

    The token IS the gate.  A caller that believes the lease carries
    ``somebody-elses-token`` must be refused outright: the file stays
    where it is, still carrying the holder's own token, and the refusal
    is written down — with the actor and the reason — so the log says
    WHO wanted the lease gone and WHY.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _write_lease(plan_dir, round_token="the-real-round")

    released = vl.force_release_verification_workflow_lease(
        plan_dir,
        expected_token="somebody-elses-token",
        actor="verification_watchdog",
        reason="verification_log_stale",
        wait_seconds=0.0,
    )

    assert released is False, (
        "a force release against a lease it does not own must be refused"
    )
    assert path.exists(), (
        "the refused force release must leave the lease file alone"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["round_token"] == "the-real-round", (
        f"the lease must still belong to its holder: {record!r}"
    )

    refusals = _events_named(
        tmp_path, plan_dir.name, "verification_lease_force_release_refused",
    )
    assert refusals, (
        "the refusal must be written down; the plan's log holds "
        f"{_plan_events(tmp_path, plan_dir.name)!r}"
    )
    data = refusals[0].get("data") or {}
    assert data.get("actor") == "verification_watchdog", (
        f"the refusal must name the actor: {data!r}"
    )
    assert data.get("reason") == "verification_log_stale", (
        f"the refusal must name the reason: {data!r}"
    )


# ---------------------------------------------------------------------------
# 2. The release — a lease that IS the one we were asked to release
# ---------------------------------------------------------------------------


def test_force_release_removes_the_file_on_token_match(tmp_path: Path) -> None:
    """The named lease is removed, and the plan is free again.

    The holder is NOT asked: its pid may well be alive (a wedged round
    is alive — that is the whole difficulty), and the rule that a live
    holder is never displaced is about GUESSING, not about an explicit,
    token-gated release by an actor that says who and why.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _write_lease(plan_dir, round_token="the-dead-round")

    released = vl.force_release_verification_workflow_lease(
        plan_dir,
        expected_token="the-dead-round",
        actor="verification_watchdog",
        reason="verification_log_stale",
        wait_seconds=0.0,
    )

    assert released is True, (
        "the lease that was named must be released"
    )
    assert not path.exists(), (
        "the lease file must be gone — the next round of this plan has "
        "to be able to take it"
    )


# ---------------------------------------------------------------------------
# 3. The order — cleanup first, unlink second
# ---------------------------------------------------------------------------


def test_force_release_runs_cleanup_before_unlinking(tmp_path: Path) -> None:
    """The holder's processes die BEFORE the lease is taken away.

    The order is the point: the wait that follows the cleanup is the
    holder's chance to notice it is being killed and give the plan back
    by itself.  Unlinking first would take the plan away from a round
    that is still running, and the two rounds would overlap — the very
    failure the lease exists to prevent.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _write_lease(plan_dir, round_token="the-dead-round")
    order: List[str] = []

    def cleanup() -> None:
        order.append("cleanup")
        order.append("lease_present" if path.exists() else "lease_gone")

    released = vl.force_release_verification_workflow_lease(
        plan_dir,
        expected_token="the-dead-round",
        actor="verification_watchdog",
        reason="verification_log_stale",
        wait_seconds=0.0,
        cleanup=cleanup,
    )

    assert released is True, "the named lease must be released"
    assert order == ["cleanup", "lease_present"], (
        f"the cleanup must run while the lease is still there, and before "
        f"it is unlinked; the recorded order was {order!r}"
    )
    assert not path.exists(), "the lease must be gone at the end"


# ---------------------------------------------------------------------------
# 4. The record — who, why, and how long it waited
# ---------------------------------------------------------------------------


def test_force_release_records_actor_reason_and_wait(tmp_path: Path) -> None:
    """The release is written down in the plan's own log.

    A force release takes the plan away from a round by hand.  The log
    is the only place that says which round lost the plan, who decided
    that, and why — so the event carries the actor, the reason, the pid
    of the holder that was displaced, and how long the call waited.
    """
    plan_dir = _plan_dir(tmp_path)
    holder_pid = os.getpid()
    _write_lease(plan_dir, round_token="the-dead-round")

    released = vl.force_release_verification_workflow_lease(
        plan_dir,
        expected_token="the-dead-round",
        actor="verification_watchdog",
        reason="verification_log_stale",
        wait_seconds=0.0,
    )
    assert released is True, "the named lease must be released"

    released_events = _events_named(
        tmp_path, plan_dir.name, "verification_lease_force_release",
    )
    assert released_events, (
        "the release must be written down; the plan's log holds "
        f"{_plan_events(tmp_path, plan_dir.name)!r}"
    )
    data = released_events[0].get("data") or {}
    assert data.get("actor") == "verification_watchdog", (
        f"the record must name the actor: {data!r}"
    )
    assert data.get("reason") == "verification_log_stale", (
        f"the record must name the reason: {data!r}"
    )
    assert data.get("holder_pid") == holder_pid, (
        f"the record must name the holder that was displaced: {data!r}"
    )
    waited = data.get("waited_seconds")
    assert isinstance(waited, (int, float)) and not isinstance(waited, bool), (
        f"the record must carry how long the release waited: {data!r}"
    )
    assert waited >= 0.0, f"the wait cannot be negative: {data!r}"


# ---------------------------------------------------------------------------
# 5. The rule that did NOT change — a live holder is still not stale
# ---------------------------------------------------------------------------


def test_live_pid_lease_is_still_considered_not_stale(tmp_path: Path) -> None:
    """A lease whose holder is alive is not stale, and still is not.

    The force release does NOT relax this.  A live holder is displaced
    only by an explicit, token-gated release that says who and why —
    never by a guess that the round looks quiet.  If staleness started
    meaning "the pid is alive but the round looks stuck", two rounds
    would overlap on one plan again.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _workflow_lease_path(plan_dir)
    now = datetime.now().isoformat()
    holder = {
        "round_token": "the-live-round",
        "run_id": plan_dir.name,
        "pid": os.getpid(),
        "started_at": now,
        "heartbeat_at": now,
    }
    path.write_text(json.dumps(holder), encoding="utf-8")

    assert vl._workflow_lease_stale_reason(path, holder) is None, (
        "a lease whose holder is alive and whose heartbeat is fresh must "
        "not be judged stale — the force release is the explicit way "
        "out, and it does not make staleness mean 'looks quiet'"
    )


# ---------------------------------------------------------------------------
# The boundary conditions around the release
# ---------------------------------------------------------------------------


def test_force_release_on_a_missing_lease_returns_false(tmp_path: Path) -> None:
    """Nothing to release is not a release.

    There is no lease file, so there is nothing to take away and
    nothing to record: the call says "no" and leaves the plan alone.
    """
    plan_dir = _plan_dir(tmp_path)

    released = vl.force_release_verification_workflow_lease(
        plan_dir,
        expected_token="whatever",
        actor="verification_watchdog",
        reason="verification_log_stale",
        wait_seconds=0.0,
    )

    assert released is False, (
        "a force release against a lease that is not there must say no"
    )
    assert not _workflow_lease_path(plan_dir).exists(), (
        "a refused release must not leave a lease behind"
    )


def test_force_release_keeps_going_when_cleanup_raises(tmp_path: Path) -> None:
    """A cleanup that blows up is recorded, and the release still happens.

    The cleanup is the caller's own — killing the round's processes.
    If it raises, the lease is STILL the one we were asked to release,
    and the release must not be lost because the cleanup failed.  The
    failure is recorded, so the log says the kill did not happen.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _write_lease(plan_dir, round_token="the-dead-round")

    def exploding_cleanup() -> None:
        raise RuntimeError("the kill blew up")

    released = vl.force_release_verification_workflow_lease(
        plan_dir,
        expected_token="the-dead-round",
        actor="verification_watchdog",
        reason="verification_log_stale",
        wait_seconds=0.0,
        cleanup=exploding_cleanup,
    )

    assert released is True, (
        "a cleanup that raised must not cost the plan its lease"
    )
    assert not path.exists(), "the lease must be gone at the end"

    released_events = _events_named(
        tmp_path, plan_dir.name, "verification_lease_force_release",
    )
    assert released_events, "the release must still be written down"
    data = released_events[0].get("data") or {}
    assert data.get("cleanup_error"), (
        f"the failed cleanup must be recorded: {data!r}"
    )


def test_force_release_does_not_unlink_a_lease_a_new_holder_took_over(
    tmp_path: Path,
) -> None:
    """The token is re-checked before the file is removed.

    The holder released the plan and the NEXT round took it while the
    force release was waiting.  That new round's lease carries a
    different token — and it is the live round now.  The force release
    must refuse it, not delete it: two rounds on one plan is exactly
    what the lease exists to prevent.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _write_lease(plan_dir, round_token="the-dead-round")

    def takeover() -> None:
        """The dead round lets go and the next round takes the plan."""
        path.unlink()
        _write_lease(plan_dir, round_token="the-new-round")

    released = vl.force_release_verification_workflow_lease(
        plan_dir,
        expected_token="the-dead-round",
        actor="verification_watchdog",
        reason="verification_log_stale",
        wait_seconds=1.0,
        cleanup=takeover,
    )

    assert released is False, (
        "the force release must refuse a lease it no longer owns"
    )
    assert path.exists(), (
        "the new holder's lease must survive — it is the live round now"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["round_token"] == "the-new-round", (
        f"the lease must still belong to the round that took over: {record!r}"
    )


def test_force_release_leaves_the_lease_alone_when_the_holder_releases(
    tmp_path: Path,
) -> None:
    """A holder that gives the plan back by itself is not unlinked.

    The wait exists for this: once the cleanup has killed the round's
    processes, the holder's own ``finally`` may release the lease.  The
    force release must then stop — there is nothing left to take away,
    and the file it would unlink may already belong to the next round.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _write_lease(plan_dir, round_token="the-dead-round")

    def holder_releases() -> None:
        """The holder's own release: the file goes away by itself."""
        path.unlink()

    released = vl.force_release_verification_workflow_lease(
        plan_dir,
        expected_token="the-dead-round",
        actor="verification_watchdog",
        reason="verification_log_stale",
        wait_seconds=1.0,
        cleanup=holder_releases,
    )

    assert released is True, (
        "the lease is gone — the plan is free, whoever took it away"
    )
    assert not path.exists(), "the lease must be gone"

    released_events = _events_named(
        tmp_path, plan_dir.name, "verification_lease_force_release",
    )
    assert released_events, "the release must be written down"
    data = released_events[0].get("data") or {}
    assert data.get("released_by_holder") is True, (
        f"the record must say the holder released it by itself: {data!r}"
    )


# ---------------------------------------------------------------------------
# The holder's own side of the same operation
# ---------------------------------------------------------------------------


def test_the_lease_can_force_release_itself(tmp_path: Path) -> None:
    """The lease object has the same force release the module has.

    The holder that knows it is wedged is the one that can say so
    precisely: it knows its own token.  Its force release must take
    the lease away, and it must stop its own heartbeat afterwards — a
    heartbeat that keeps running on a lease that is gone is a thread
    that never ends.
    """
    plan_dir = _plan_dir(tmp_path)

    lease, conflict = vl._acquire_verification_workflow_lease(
        plan_dir, round_token="the-wedged-round", run_id=plan_dir.name,
    )
    assert conflict is None, f"the first holder must not be refused: {conflict!r}"
    assert lease is not None, "the first holder must get a lease"

    released = lease.force_release(
        actor="verification_watchdog",
        reason="verification_log_stale",
        wait_seconds=0.0,
    )

    assert released is True, "the lease must be released"
    assert not _workflow_lease_path(plan_dir).exists(), (
        "the lease file must be gone"
    )
    assert lease._thread is None, (
        "the heartbeat thread must be stopped — it would keep running on "
        "a lease that is gone"
    )
    assert lease._released is True, (
        "the lease must know it gave the plan back"
    )
