"""The plan-level verification-round lease (2026-10-25).

The criterion this file pins
----------------------------
The import acceptance criterion for the capacity-wait work reads:

    *the progress view keeps the interrupted task ``in_progress`` and its
    two downstream tasks ``pending``*

That is only a fact when exactly ONE round is writing the plan's
``state.db``.  On 2026-10-24 three verification rounds of the same plan
overlapped (21:18, 21:42, 21:45).  The 21:42 round emitted no
``services_ensured`` event at all and finished its nine ``api_test`` VPs
between 21:53:05 and 21:53:21, while the 21:45 round's own
``services_ensured`` only completed at 21:54:08 — so one round's VPs ran
against a service that was not up yet, reading a fixture another round
was rewriting underneath them.

What this file fixes
--------------------
A plan-level EXCLUSIVE lease, taken before the round's preflight:

  * the lock is ``plans/<plan_id>/verification_round.lock``, created with
    ``O_CREAT | O_EXCL`` — the creation itself is the atomic step, so
    two runs racing for the same plan cannot both believe they won;
  * it records the holder's ``pid``, ``started_at`` and ``heartbeat``;
  * a run that cannot take it waits a BOUNDED number of seconds and then
    ends with the reason ``round_already_running``, dispatching NO VP;
  * the holder releases it in a ``finally``, so every exit path gives the
    plan back;
  * a lock whose recorded pid is dead is STALE: it is broken, with a log
    line, rather than waited on — so a round that was killed does not
    lock its plan out forever.

Hermeticity
------------
Everything happens inside ``tmp_path``: the lock file is the only thing
written, and it is written into the test's own plan directory.  No
process is spawned except the one-shot that produces a pid the operating
system has already taken back (the "dead pid" the stale-lock test
needs).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import server  # noqa: F401 — seeds sys.modules['server'] before the import
import verification_loop as vl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_dir(tmp_path: Path, plan_id: str = "20261024-vp-quota-parked") -> Path:
    """The plan's directory — the lease lives inside it."""
    path = tmp_path / "plans" / plan_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _lock_path(plan_dir: Path) -> Path:
    return plan_dir / vl._VERIFICATION_ROUND_LOCK_FILENAME


def _dead_pid() -> int:
    """A pid the operating system has already handed back.

    A real process is started and waited for, so the number returned is
    one the OS has already taken back.  The assertion at the end is what
    makes that a fact rather than a hope: if the pid were somehow alive
    again, the test says so instead of probing a stranger's process.
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


class _FakeOrchestrator:
    """The one method of the orchestrator the lease touches."""

    def __init__(self, report: Optional[Dict[str, Any]] = None,
                 raises: bool = False) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.report = {"verification_results": []} if report is None else report
        self.raises = raises

    def start_verification_cycle(
        self, round_number: int, force: bool = False, resume: bool = False,
    ) -> Dict[str, Any]:
        self.calls.append({
            "round_number": round_number, "force": force, "resume": resume,
        })
        if self.raises:
            raise RuntimeError("the round blew up")
        return self.report


# ---------------------------------------------------------------------------
# 1. The lease exists and says who holds it
# ---------------------------------------------------------------------------


def test_acquire_creates_the_lock_file_with_pid_started_at_and_heartbeat(
    tmp_path: Path,
) -> None:
    """The lock file is the lease, and it names its holder.

    The record has to carry the pid (so a reader can tell a live holder
    from a corpse), the start time (so a reader can tell how long the
    round has been running) and a heartbeat (so a reader can tell a
    holder that is still making progress from one that has wedged).
    """
    plan_dir = _plan_dir(tmp_path)

    lease, conflict = vl._acquire_verification_round_lease(plan_dir)
    try:
        assert conflict is None, f"the first holder must not be refused: {conflict!r}"
        assert lease is not None, "the first holder must get a lease"

        path = _lock_path(plan_dir)
        assert path.exists(), (
            f"the lease must live at {path} — the plan's directory is "
            f"where every other round artefact lives"
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["pid"] == os.getpid(), (
            f"the lock must record the holder's pid; got {record!r}"
        )
        assert record["started_at"], f"the lock must record a start time: {record!r}"
        assert record["heartbeat"], f"the lock must record a heartbeat: {record!r}"
    finally:
        lease.release()


# ---------------------------------------------------------------------------
# 2. A second run is refused — explicitly, and without dispatching a VP
# ---------------------------------------------------------------------------


def test_second_acquire_is_refused_with_round_already_running(
    tmp_path: Path,
) -> None:
    """The loser is told WHY it lost.

    ``round_already_running`` is the reason the run ends on, so it has
    to be the reason the lease reports — an operator (and the log) must
    be able to tell "somebody else is verifying this plan" apart from
    "the plan is broken".
    """
    plan_dir = _plan_dir(tmp_path)

    lease, conflict = vl._acquire_verification_round_lease(plan_dir)
    try:
        assert lease is not None
        loser, conflict = vl._acquire_verification_round_lease(
            plan_dir, wait_seconds=0.0,
        )
        assert loser is None, "a second holder must not be handed the lease"
        assert conflict is not None, "the refusal must carry a reason"
        assert conflict["reason"] == "round_already_running", (
            f"unexpected refusal reason: {conflict!r}"
        )
        assert conflict["holder_pid"] == os.getpid(), (
            f"the refusal must name the holder; got {conflict!r}"
        )
        # The winner's lock is untouched — the loser did not steal it.
        record = json.loads(_lock_path(plan_dir).read_text(encoding="utf-8"))
        assert record["pid"] == os.getpid()
    finally:
        lease.release()


def test_refused_acquire_waits_at_most_the_configured_seconds(
    tmp_path: Path,
) -> None:
    """The wait is bounded.

    A run that waits forever for a lease nobody will give it is a hung
    run holding the plan just as badly as an overlapping one.  The
    refusal has to arrive on its own.
    """
    plan_dir = _plan_dir(tmp_path)

    lease, _ = vl._acquire_verification_round_lease(plan_dir)
    try:
        started = time.monotonic()
        loser, conflict = vl._acquire_verification_round_lease(
            plan_dir, wait_seconds=0.4, poll_seconds=0.05,
        )
        elapsed = time.monotonic() - started
        assert loser is None
        assert conflict is not None
        assert conflict["reason"] == "round_already_running"
        assert elapsed >= 0.3, (
            f"the waiter gave up after {elapsed:.3f}s — it did not wait "
            f"the configured window at all"
        )
        assert elapsed < 5.0, (
            f"the waiter took {elapsed:.3f}s — the wait is not bounded"
        )
    finally:
        lease.release()


# ---------------------------------------------------------------------------
# 3. The lease is given back
# ---------------------------------------------------------------------------


def test_release_removes_the_lock_file(tmp_path: Path) -> None:
    """A finished round hands the plan back.

    Without this the plan would stay locked until the holder's pid
    died — which, for a long-lived backend, is never.
    """
    plan_dir = _plan_dir(tmp_path)

    lease, _ = vl._acquire_verification_round_lease(plan_dir)
    assert _lock_path(plan_dir).exists()

    lease.release()

    assert not _lock_path(plan_dir).exists(), (
        "the lease must be released — the next round of this plan has "
        "to be able to run"
    )


def test_release_leaves_a_lock_owned_by_somebody_else_alone(
    tmp_path: Path,
) -> None:
    """A displaced holder must not delete the new holder's lock.

    The release is guarded by the token recorded in the file: whoever
    holds the lease may give it back, and nobody else may.
    """
    plan_dir = _plan_dir(tmp_path)

    lease, _ = vl._acquire_verification_round_lease(plan_dir)
    path = _lock_path(plan_dir)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["token"] = "somebody-elses-token"
    path.write_text(json.dumps(record), encoding="utf-8")

    lease.release()

    assert path.exists(), (
        "the release must not remove a lock this holder does not own"
    )
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == (
        "somebody-elses-token"
    )


# ---------------------------------------------------------------------------
# 4. A dead holder does not lock the plan out forever
# ---------------------------------------------------------------------------


def test_stale_lock_with_a_dead_pid_is_broken(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A lock whose holder is gone is broken, with a log line.

    The holder died without running its ``finally`` — killed, crashed,
    or the machine went down.  Waiting for it would wait forever, so the
    lease breaks it and says so.
    """
    import logging

    plan_dir = _plan_dir(tmp_path)
    path = _lock_path(plan_dir)
    path.write_text(
        json.dumps({
            "plan_id": plan_dir.name,
            "pid": _dead_pid(),
            "started_at": "2026-10-24T21:42:00",
            "heartbeat": "2026-10-24T21:42:00",
            "token": "the-dead-round's-token",
        }),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        lease, conflict = vl._acquire_verification_round_lease(
            plan_dir, wait_seconds=0.0,
        )
    try:
        assert conflict is None, (
            f"a stale lock must not refuse the round: {conflict!r}"
        )
        assert lease is not None, "the stale lock must be broken, not obeyed"
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["pid"] == os.getpid(), (
            f"the broken lock must have been replaced by the new holder; "
            f"got {record!r}"
        )
        assert "stale" in caplog.text.lower(), (
            f"breaking a stale lock must be logged; the log said: "
            f"{caplog.text!r}"
        )
    finally:
        lease.release()


def test_lock_with_a_live_pid_is_not_broken(tmp_path: Path) -> None:
    """A live holder is never displaced.

    The lease's contract is "a live process holds this".  Breaking a
    live holder's lock is the one failure this mechanism must not have
    — it would put two rounds back on the same plan.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _lock_path(plan_dir)
    path.write_text(
        json.dumps({
            "plan_id": plan_dir.name,
            "pid": os.getpid(),
            "started_at": "2026-10-24T21:42:00",
            "heartbeat": "2026-10-24T21:42:00",
            "token": "a-live-round's-token",
        }),
        encoding="utf-8",
    )

    loser, conflict = vl._acquire_verification_round_lease(
        plan_dir, wait_seconds=0.0,
    )

    assert loser is None
    assert conflict is not None
    assert conflict["reason"] == "round_already_running"
    assert path.exists(), "a live holder's lock must survive the attempt"
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == (
        "a-live-round's-token"
    )


# ---------------------------------------------------------------------------
# 5. The round dispatch goes through the lease
# ---------------------------------------------------------------------------


def test_dispatch_refuses_to_run_the_round_when_the_lease_is_held(
    tmp_path: Path,
) -> None:
    """No VP is dispatched by the run that lost the race.

    This is the whole point of the lease: the second round must not
    reach the orchestrator, because the orchestrator is what starts the
    services, seeds the fixture and runs the VPs.
    """
    plan_dir = _plan_dir(tmp_path)
    lease, _ = vl._acquire_verification_round_lease(plan_dir)
    orch = _FakeOrchestrator()
    try:
        report, conflict = vl._dispatch_verification_round(
            plan_dir.name, plan_dir, orch, 1, resume=False,
            wait_seconds=0.0,
        )
        assert report is None, "a refused round must not produce a report"
        assert conflict is not None, "a refused round must carry a reason"
        assert conflict["reason"] == "round_already_running"
        assert orch.calls == [], (
            f"the orchestrator must not be called by a refused round; "
            f"it was called with {orch.calls!r}"
        )
    finally:
        lease.release()


def test_dispatch_runs_the_round_when_the_lease_is_free(
    tmp_path: Path,
) -> None:
    """The happy path still works — and the lease is given back after it."""
    plan_dir = _plan_dir(tmp_path)
    orch = _FakeOrchestrator(report={"verification_results": [{"status": "PASSED"}]})

    report, conflict = vl._dispatch_verification_round(
        plan_dir.name, plan_dir, orch, 2, resume=True,
    )

    assert conflict is None, f"the round must not be refused: {conflict!r}"
    assert report == {"verification_results": [{"status": "PASSED"}]}
    assert orch.calls == [
        {"round_number": 2, "force": False, "resume": True},
    ], f"the round must be dispatched with the caller's arguments: {orch.calls!r}"
    assert not _lock_path(plan_dir).exists(), (
        "the lease must be released once the round is dispatched"
    )


def test_dispatch_releases_the_lease_when_the_round_raises(
    tmp_path: Path,
) -> None:
    """A round that blows up still gives the plan back."""
    plan_dir = _plan_dir(tmp_path)
    orch = _FakeOrchestrator(raises=True)

    with pytest.raises(RuntimeError):
        vl._dispatch_verification_round(
            plan_dir.name, plan_dir, orch, 1, resume=False,
        )

    assert not _lock_path(plan_dir).exists(), (
        "a round that raised must not keep the plan locked"
    )


def test_two_dispatches_of_the_same_plan_cannot_overlap(
    tmp_path: Path,
) -> None:
    """Two rounds of one plan are serialised.

    The first dispatch holds the lease while its VPs run; the second
    one is refused.  This is the incident in miniature: without the
    lease both rounds run at once, each against a service the other
    one is still preparing.
    """
    plan_dir = _plan_dir(tmp_path)

    lease, _ = vl._acquire_verification_round_lease(plan_dir)
    first = _FakeOrchestrator()
    second = _FakeOrchestrator()
    try:
        report, conflict = vl._dispatch_verification_round(
            plan_dir.name, plan_dir, second, 1, resume=False,
            wait_seconds=0.0,
        )
        assert report is None
        assert conflict is not None
        assert conflict["reason"] == "round_already_running"
        assert second.calls == [], (
            f"the second round must not dispatch anything; it dispatched "
            f"{second.calls!r}"
        )
        assert first.calls == []
    finally:
        lease.release()


def test_second_round_is_refused_while_the_first_holds_the_lock(
    tmp_path: Path,
) -> None:
    """The second round of a plan is refused while the first holds the lock.

    This is the guarantee stated at the moment it matters: the first
    round is IN FLIGHT -- it is inside ``start_verification_cycle``,
    which is where its VPs run and where the plan's lock is held -- and
    the second round of the same plan arrives.

    The second round must be refused outright: no report, no
    orchestrator call, and a reason that says a round was already
    running.  Anything less lets two rounds of one plan write the same
    ``state.db`` at the same time, which is what the 2026-10-24 incident
    was.
    """
    plan_dir = _plan_dir(tmp_path)
    second = _FakeOrchestrator()
    seen: Dict[str, Any] = {}

    class _InFlightRound:
        """A round that is running right now -- it holds the plan's lock."""

        def __init__(self) -> None:
            self.calls: List[Dict[str, Any]] = []

        def start_verification_cycle(
            self, round_number: int, force: bool = False, resume: bool = False,
        ) -> Dict[str, Any]:
            self.calls.append({
                "round_number": round_number, "force": force, "resume": resume,
            })
            # The round is in flight -- the second one asks to run NOW.
            report, conflict = vl._dispatch_verification_round(
                plan_dir.name, plan_dir, second, 2, resume=True,
                wait_seconds=0.0,
            )
            seen["report"] = report
            seen["conflict"] = conflict
            return {"verification_results": [{"status": "PASSED"}]}

    first = _InFlightRound()

    report, conflict = vl._dispatch_verification_round(
        plan_dir.name, plan_dir, first, 1, resume=False,
    )

    assert conflict is None, (
        f"the first round must not be refused: {conflict!r}"
    )
    assert report == {"verification_results": [{"status": "PASSED"}]}
    assert seen["report"] is None, (
        "the second round must not produce a report while the first "
        "holds the lock"
    )
    assert seen["conflict"] is not None, (
        "the second round must be told WHY it was refused"
    )
    assert seen["conflict"]["reason"] == "round_already_running", (
        f"unexpected refusal reason: {seen['conflict']!r}"
    )
    assert second.calls == [], (
        f"the second round must not dispatch a VP while the first holds "
        f"the lock; it dispatched {second.calls!r}"
    )
    assert first.calls == [
        {"round_number": 1, "force": False, "resume": False},
    ], f"the first round must have been dispatched: {first.calls!r}"
    assert not _lock_path(plan_dir).exists(), (
        "the plan must be handed back once the first round is done"
    )


# ---------------------------------------------------------------------------
# 6. The heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_refreshes_the_recorded_timestamp(tmp_path: Path) -> None:
    """The holder's liveness stamp moves while it is alive.

    The heartbeat is what tells a reader "the holder is still working"
    apart from "the holder wedged ten minutes ago" — the pid alone
    cannot tell those apart.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _lock_path(plan_dir)

    lease, _ = vl._acquire_verification_round_lease(plan_dir)
    try:
        before = json.loads(path.read_text(encoding="utf-8"))
        lease.started_at = "2000-01-01T00:00:00"
        # The recorded heartbeat moves even when the clock has not.
        lease.heartbeat()
        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["token"] == before["token"], (
            f"the heartbeat must not change the holder's identity; "
            f"got {after!r}"
        )
        assert after["heartbeat"] != before["heartbeat"] or (
            after["heartbeat"] >= before["heartbeat"]
        ), f"the heartbeat must be refreshed: {after!r}"
    finally:
        lease.release()


def test_heartbeat_refuses_to_write_after_release(tmp_path: Path) -> None:
    """A released holder cannot resurrect its lock.

    The write opens the file without ``O_CREAT`` precisely so that a
    heartbeat that fires after the release cannot recreate the lock the
    holder gave back.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _lock_path(plan_dir)

    lease, _ = vl._acquire_verification_round_lease(plan_dir)
    lease.release()

    assert lease.heartbeat() is False, (
        "a released lease must refuse to heartbeat"
    )
    assert not path.exists(), (
        "a heartbeat after the release must not recreate the lock"
    )


# ---------------------------------------------------------------------------
# 7. The workflow-level round lease (VP-008, 2026-10-25)
# ---------------------------------------------------------------------------
#
# The lease above guards ONE dispatch: the window in which
# ``start_verification_cycle`` runs the VPs.  It does NOT guard the
# WORKFLOW — between two dispatches the loop runs its judgment phase,
# writes the round's verdict and may chain into a repair, and a second
# round of the same plan can take the dispatch lease inside that gap.
#
# On 2026-10-24 that is exactly what happened.  Three rounds of one
# plan overlapped (21:18, 21:42, 21:45); the 21:42 round emitted no
# ``services_ensured`` event at all and finished its nine ``api_test``
# VPs between 21:53:05 and 21:53:21, while the 21:45 round's own
# ``services_ensured`` only completed at 21:54:08.  So one round's VPs
# ran against a service that was not up yet, reading a fixture another
# round was rewriting underneath them: two rounds, two views of the
# same plan, and no single authority over either the fixture or
# ``state.db``.
#
# The workflow lease is that authority.  It is taken at the ENTRY of
# ``_run_auto_verification_loop`` — before the round's preflight,
# before the services are ensured, before a single VP is dispatched —
# and it is given back when the workflow ends.
#
# Its ``round_token`` is the round's SERVICE OWNER token: the same
# token the ledger stamps on the services the round starts and the
# same token the round's pin (VP-004) carries.  One round, one token,
# one authority over the services it uses.


def _workflow_lease_path(plan_dir: Path) -> Path:
    """Where the workflow-level lease lives."""
    return plan_dir / vl._VERIFICATION_WORKFLOW_LEASE_FILENAME


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


def _stub_loop_body(monkeypatch) -> Dict[str, Any]:
    """Replace the loop body; return the record of what it was asked to do.

    The body is what builds the orchestrator, ensures the services and
    dispatches the VPs — so "the body never ran" is the strongest
    statement this file can make about a refused round.
    """
    seen: Dict[str, Any] = {"inner_calls": [], "reap_calls": []}

    def fake_inner(plan_id, plan_dir, project_dir, **kwargs):
        seen["inner_calls"].append((plan_id, str(plan_dir)))
        return None

    monkeypatch.setattr(server, "_run_auto_verification_loop_inner", fake_inner)
    monkeypatch.setattr(
        server, "_reap_managed_services",
        lambda plan_id, plan_dir, reason, **kwargs: seen["reap_calls"].append(
            reason
        ),
    )
    return seen


def test_workflow_lease_records_the_round_token_run_id_pid_and_timestamps(
    tmp_path: Path,
) -> None:
    """The lease names its holder, in the shape the round contract asks for.

    ``round_token`` is the round's service-owner token — the same one
    the ledger stamps on the services this round starts and the same
    one the round's pin carries — so a reader can tie the lease to the
    services without guessing.  ``pid`` says whether the holder is
    still there; ``heartbeat_at`` says whether it is still working.
    """
    plan_dir = _plan_dir(tmp_path)

    lease, conflict = vl._acquire_verification_workflow_lease(
        plan_dir, round_token="round-token-abc", run_id=plan_dir.name,
    )
    try:
        assert conflict is None, (
            f"the first holder must not be refused: {conflict!r}"
        )
        path = _workflow_lease_path(plan_dir)
        assert path.exists(), (
            f"the workflow lease must live at {path} — beside the plan's "
            f"other round artefacts"
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["round_token"] == "round-token-abc", (
            f"the lease must record the round's token: {record!r}"
        )
        assert record["run_id"] == plan_dir.name, (
            f"the lease must record the run it belongs to: {record!r}"
        )
        assert record["pid"] == os.getpid(), (
            f"the lease must record the holder's pid: {record!r}"
        )
        assert record["started_at"], f"the lease must record a start time: {record!r}"
        assert record["heartbeat_at"], (
            f"the lease must record a heartbeat: {record!r}"
        )
    finally:
        lease.release()


def test_a_round_that_cannot_take_the_lease_dispatches_no_vp(
    tmp_path: Path, monkeypatch,
) -> None:
    """The loser of the race must not reach the orchestrator.

    Reaching the orchestrator is what starts the services, seeds the
    fixture and runs the VPs — the three things two rounds of one plan
    must not do at the same time.
    """
    plan_dir = _plan_dir(tmp_path)
    holder, _ = vl._acquire_verification_workflow_lease(
        plan_dir, round_token="the-other-round", run_id=plan_dir.name,
    )
    seen = _stub_loop_body(monkeypatch)
    try:
        vl._run_auto_verification_loop(plan_dir.name, plan_dir, tmp_path)
    finally:
        holder.release()

    assert seen["inner_calls"] == [], (
        f"the refused round reached the loop body; it ran "
        f"{seen['inner_calls']!r}"
    )
    assert seen["reap_calls"] == [], (
        "the refused round reaped the plan's services — those belong to "
        "the round that owns the plan"
    )


def test_the_refused_round_is_marked_blocked_with_round_conflict(
    tmp_path: Path, monkeypatch,
) -> None:
    """The refusal is a BLOCKED round, not a failed one.

    Nothing was measured — no service was ensured, no VP ran — so
    there is nothing to repair and nothing to fail.  The record says
    exactly that, and it names the holder that owns the plan.
    """
    plan_dir = _plan_dir(tmp_path)
    holder, _ = vl._acquire_verification_workflow_lease(
        plan_dir, round_token="the-other-round", run_id=plan_dir.name,
    )
    _stub_loop_body(monkeypatch)
    try:
        vl._run_auto_verification_loop(plan_dir.name, plan_dir, tmp_path)
    finally:
        holder.release()

    events = _plan_events(tmp_path, plan_dir.name)
    denied = [e for e in events if e.get("event") == "round_lease_denied"]
    assert denied, (
        f"the refusal must be recorded; the plan's log holds {events!r}"
    )
    assert denied[0]["data"]["holder"]["round_token"] == "the-other-round", (
        f"the refusal must name the holder that owns the plan: "
        f"{denied[0]!r}"
    )
    blocked = [
        e for e in events if e.get("event") == "verification_round_blocked"
    ]
    assert blocked, (
        f"the round must be marked BLOCKED; the plan's log holds {events!r}"
    )
    data = blocked[0]["data"]
    assert data["status"] == "BLOCKED", (
        f"the round must be marked BLOCKED, not failed: {data!r}"
    )
    assert data["error_kind"] == "round_conflict", (
        f"the round must say WHY it was blocked: {data!r}"
    )


def test_the_lease_shares_its_round_token_with_the_round_pin(
    tmp_path: Path, monkeypatch,
) -> None:
    """One round, one token: the lease and the pin name the same round.

    The pin's token is the plan's *current service owner* — that is
    what ``record_runtimes`` stamps on the services the round starts
    and what ``pin_services_for_round`` writes into the pin file.  If
    the lease named a different round, a reader could not tell which
    round's services the lease was protecting.
    """
    import service_manager as sm

    plan_dir = _plan_dir(tmp_path)
    seen: Dict[str, Any] = {}

    def fake_inner(plan_id, plan_dir_arg, project_dir, **kwargs):
        record = json.loads(
            _workflow_lease_path(plan_dir).read_text(encoding="utf-8"),
        )
        seen["lease_round_token"] = record["round_token"]
        seen["current_service_owner"] = sm.current_service_owner(plan_dir)
        return None

    monkeypatch.setattr(server, "_run_auto_verification_loop_inner", fake_inner)
    monkeypatch.setattr(server, "_reap_managed_services", lambda *a, **k: None)

    vl._run_auto_verification_loop(plan_dir.name, plan_dir, tmp_path)

    assert seen["lease_round_token"], (
        "the lease must name the round it holds"
    )
    assert seen["lease_round_token"] == seen["current_service_owner"], (
        f"the lease's round token ({seen['lease_round_token']!r}) and the "
        f"round's service owner ({seen['current_service_owner']!r}) must "
        f"be one token — that is the token the round's pin carries"
    )


def test_the_workflow_lease_is_released_when_the_workflow_ends(
    tmp_path: Path, monkeypatch,
) -> None:
    """A finished workflow hands the plan back.

    Without this the plan would stay locked until the holder's pid
    died — which, for a long-lived backend, is never.
    """
    plan_dir = _plan_dir(tmp_path)
    _stub_loop_body(monkeypatch)

    vl._run_auto_verification_loop(plan_dir.name, plan_dir, tmp_path)

    assert not _workflow_lease_path(plan_dir).exists(), (
        "the lease must be released — the next round of this plan has to "
        "be able to run"
    )


def test_a_lease_whose_heartbeat_went_stale_is_taken_over(
    tmp_path: Path,
) -> None:
    """A holder that stopped saying it is alive does not own the plan.

    The heartbeat is the holder's liveness stamp.  When it goes stale
    the round behind it is gone — wedged, killed, its thread died — and
    the next round must be able to take the plan over.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _workflow_lease_path(plan_dir)
    stale = (
        datetime.now()
        - timedelta(seconds=vl._VERIFICATION_WORKFLOW_LEASE_STALE_SECONDS + 60)
    ).isoformat()
    path.write_text(
        json.dumps({
            "round_token": "the-stale-round",
            "run_id": plan_dir.name,
            "pid": os.getpid(),
            "started_at": stale,
            "heartbeat_at": stale,
        }),
        encoding="utf-8",
    )

    lease, conflict = vl._acquire_verification_workflow_lease(
        plan_dir, round_token="the-new-round", run_id=plan_dir.name,
    )
    try:
        assert conflict is None, (
            f"a stale lease must not refuse the round: {conflict!r}"
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["round_token"] == "the-new-round", (
            f"the stale lease must have been replaced by the new holder: "
            f"{record!r}"
        )
    finally:
        lease.release()


def test_a_lease_whose_holder_is_alive_is_not_taken_over(
    tmp_path: Path,
) -> None:
    """A live holder is never displaced.

    The lease's contract is "a live round holds this".  Breaking a
    live holder's lease is the one failure this mechanism must not
    have — it would put two rounds back on the same plan.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _workflow_lease_path(plan_dir)
    now = datetime.now().isoformat()
    path.write_text(
        json.dumps({
            "round_token": "the-live-round",
            "run_id": plan_dir.name,
            "pid": os.getpid(),
            "started_at": now,
            "heartbeat_at": now,
        }),
        encoding="utf-8",
    )

    lease, conflict = vl._acquire_verification_workflow_lease(
        plan_dir, round_token="the-new-round", run_id=plan_dir.name,
    )

    assert lease is None, "a live holder's lease must not be taken"
    assert conflict is not None, "the refusal must carry a reason"
    assert conflict["reason"] == "round_conflict", (
        f"unexpected refusal reason: {conflict!r}"
    )
    assert conflict["holder"]["round_token"] == "the-live-round", (
        f"the refusal must name the holder: {conflict!r}"
    )


def test_a_lease_whose_holder_is_dead_is_taken_over(tmp_path: Path) -> None:
    """A holder that is gone does not own the plan.

    The holder died without releasing the lease — killed, crashed, or
    the machine went down.  Waiting for it would wait forever.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _workflow_lease_path(plan_dir)
    now = datetime.now().isoformat()
    path.write_text(
        json.dumps({
            "round_token": "the-dead-round",
            "run_id": plan_dir.name,
            "pid": _dead_pid(),
            "started_at": now,
            "heartbeat_at": now,
        }),
        encoding="utf-8",
    )

    lease, conflict = vl._acquire_verification_workflow_lease(
        plan_dir, round_token="the-new-round", run_id=plan_dir.name,
    )
    try:
        assert conflict is None, (
            f"a dead holder's lease must not refuse the round: {conflict!r}"
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["round_token"] == "the-new-round", (
            f"the dead holder's lease must have been replaced: {record!r}"
        )
    finally:
        lease.release()


# ---------------------------------------------------------------------------
# The plan's acceptance nodes — the second hard constraint, under the names
# the plan gates on
# ---------------------------------------------------------------------------
#
# The repair task for VP-008 gates itself on four nodes of THIS file, under
# the names the plan states:
#
#     test_second_round_denied_while_lease_held
#     test_lease_acquired_when_stale
#     test_lease_released_in_finally
#     test_pin_and_lease_share_round_token
#
# The criterion behind them is this plan's SECOND hard constraint: one plan,
# one verification round at a time, and the round's services named by the
# round's own token.  The tests above pin each piece of that criterion
# under the name the mechanism was given when it was written; these four
# pin the SAME criterion under the names the plan gates on, so that the gate
# answers a question about the plan rather than about a naming convention.
#
# They are written as tests of their own rather than as aliases of the ones
# above: an alias would make the gate green without making the criterion
# any more true, and a gate that cannot fail is not a gate.


def test_second_round_denied_while_lease_held(tmp_path: Path) -> None:
    """A second round is denied while the first one holds the lease.

    The lease is what makes "one plan, one round at a time" true, and the
    denial has to be visible to the round that lost: it gets no lease, it
    gets the reason, and it does not get to take the lock file away from
    the round that won.
    """
    plan_dir = _plan_dir(tmp_path)

    first, first_conflict = vl._acquire_verification_round_lease(plan_dir)
    assert first_conflict is None, (
        f"the first round must take the lease: {first_conflict!r}"
    )
    assert first is not None, "the first round must hold the lease"
    try:
        second, conflict = vl._acquire_verification_round_lease(
            plan_dir, wait_seconds=0.0,
        )

        assert second is None, (
            "the second round must not get a lease while the first holds it"
        )
        assert conflict is not None, (
            "the denied round must be told WHY it was denied"
        )
        assert conflict["reason"] == "round_already_running", (
            f"unexpected denial reason: {conflict!r}"
        )
        assert conflict["holder_pid"] == os.getpid(), (
            f"the conflict must name the holder it lost to: {conflict!r}"
        )
        # The loser must not have taken the lock file over: the lock still
        # belongs to the round that won, and only that round may release it.
        record = json.loads(_lock_path(plan_dir).read_text(encoding="utf-8"))
        assert record["token"] == first.token, (
            f"the lock file must still belong to the holder: {record!r}"
        )
    finally:
        first.release()


def test_lease_acquired_when_stale(tmp_path: Path) -> None:
    """A stale lease is taken over — the next round gets the plan.

    A lease whose heartbeat went stale belongs to a round that is gone:
    wedged, killed, its thread died.  Nothing else will ever release it,
    so the next round has to be able to take it — otherwise one wedged
    round locks its plan out forever.

    The holder's pid is this process — alive — so the heartbeat is the
    ONLY reason the lease is stale.  A lease that is stale for any other
    reason (a dead pid) is a different case, and this one must not pass
    by accident.
    """
    plan_dir = _plan_dir(tmp_path)
    path = _workflow_lease_path(plan_dir)
    stale = (
        datetime.now()
        - timedelta(seconds=vl._VERIFICATION_WORKFLOW_LEASE_STALE_SECONDS + 60)
    ).isoformat()
    path.write_text(
        json.dumps({
            "round_token": "the-stale-round",
            "run_id": plan_dir.name,
            "pid": os.getpid(),
            "started_at": stale,
            "heartbeat_at": stale,
        }),
        encoding="utf-8",
    )

    lease, conflict = vl._acquire_verification_workflow_lease(
        plan_dir, round_token="the-new-round", run_id=plan_dir.name,
    )
    try:
        assert conflict is None, (
            f"a stale lease must not refuse the next round: {conflict!r}"
        )
        assert lease is not None, "the next round must hold the lease"
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["round_token"] == "the-new-round", (
            f"the stale lease must have been replaced by the new holder: "
            f"{record!r}"
        )
    finally:
        if lease is not None:
            lease.release()


def test_lease_released_in_finally(tmp_path: Path, monkeypatch) -> None:
    """The lease is given back even when the round blows up.

    The release sits in a ``finally``, so it happens on EVERY exit path —
    including the one where the round raised.  A lease that survives its
    round's crash is a plan locked out until the holder's pid dies, and
    for a long-lived backend that is never.
    """
    plan_dir = _plan_dir(tmp_path)

    def exploding_inner(plan_id, plan_dir_arg, project_dir, **kwargs):
        raise RuntimeError("the round blew up")

    monkeypatch.setattr(
        server, "_run_auto_verification_loop_inner", exploding_inner,
    )
    monkeypatch.setattr(
        server, "_reap_managed_services", lambda *a, **k: None,
    )

    with pytest.raises(RuntimeError):
        vl._run_auto_verification_loop(plan_dir.name, plan_dir, tmp_path)

    assert not _workflow_lease_path(plan_dir).exists(), (
        "the lease must be released even when the round raised — the next "
        "round of this plan has to be able to run"
    )


def test_pin_and_lease_share_round_token(
    tmp_path: Path, monkeypatch,
) -> None:
    """The pin and the lease name the same round.

    Three things name a round: the lease it holds, the owner token its
    services are stamped with, and the pin that protects those services
    from a reap.  A reader that cannot tell they are the same round
    cannot tell which services the lease is protecting — and a reap that
    reads the wrong round's pin stops the wrong services.
    """
    import service_manager as sm

    plan_dir = _plan_dir(tmp_path)
    seen: Dict[str, Any] = {}

    def fake_inner(plan_id, plan_dir_arg, project_dir, **kwargs):
        record = json.loads(
            _workflow_lease_path(plan_dir).read_text(encoding="utf-8"),
        )
        seen["lease_round_token"] = record["round_token"]
        seen["current_service_owner"] = sm.current_service_owner(plan_dir)
        runtime = sm.ServiceRuntime(
            name="api", port=12345, pid=os.getpid(), ready=True,
        )
        sm.pin_services_for_round(
            plan_dir, record["round_token"], [runtime],
        )
        return None

    monkeypatch.setattr(server, "_run_auto_verification_loop_inner", fake_inner)
    monkeypatch.setattr(server, "_reap_managed_services", lambda *a, **k: None)

    vl._run_auto_verification_loop(plan_dir.name, plan_dir, tmp_path)

    assert seen["lease_round_token"], (
        "the lease must name the round it holds"
    )
    assert seen["lease_round_token"] == seen["current_service_owner"], (
        f"the lease's round token ({seen['lease_round_token']!r}) and the "
        f"round's service owner ({seen['current_service_owner']!r}) must be "
        f"one token"
    )
    pins = sm.read_round_pins(plan_dir)
    assert pins, "the round must have pinned the service it started"
    assert [pin.get("round_token") for pin in pins] == [
        seen["lease_round_token"],
    ], (
        f"the pin must carry the lease's round token — one round, one "
        f"token: {pins!r}"
    )
