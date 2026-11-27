"""A verification round the server took down with it comes back as queued.

Background
----------
The shutdown path leaves a checkpoint behind for every plan that was
still in flight (``backend/shutdown_checkpoint.py``).  The startup scan
has to answer the same question the execution side answers: *was this
interrupted by the server going down, or did the work die?*

Before this change the verification recovery scan
(:func:`verification_loop._recover_verification_states`) only ever looked
at ``running`` rounds, and it never touched the plan's phase.  A plan
whose verification was interrupted by the shutdown therefore stayed at
``verification_running`` for ever: the scheduler could not see it, and
nothing could pick the round up again.

The writer side covers three in-flight statuses -- ``running``,
``repairing`` and ``rerunning`` (``server._VERIFICATION_IN_FLIGHT_STATUSES``)
-- so the reader side has to cover the same three.  Otherwise the
checkpoints written for the other two statuses would never be consumed.

What these tests pin
--------------------
* a fresh shutdown checkpoint requeues the plan (phase becomes ``queued``);
* the round the plan had reached survives the requeue;
* no checkpoint, or a stale one, leaves the existing recovery exactly as
  it was: the phase untouched and the ``running`` entry still recovered;
* the same two cases for a ``repairing`` round -- requeued when the
  checkpoint is fresh, left alone when it is stale, and never given an
  in-memory entry (only a ``running`` round was ever loaded into memory).

The phase assertions read the real ``plan_routing`` row back through
:class:`plan_state.PlanState`, so they say what a reader of the plan
would see -- not what a mock was told to answer.

2026-11-13 (VP-027) added the other half of the same race: the writer
that was racing the shutdown stamped ``failed`` over a round the
shutdown had already interrupted.  Those tests pin that the failure
does not land at all (the plan goes back to the queue, and no verdict
is written), that the recovery scan recognises a ``failed`` the second
writer left behind -- and takes it back with the requeue -- and that
the guard does not swallow a verdict worth keeping (a pass still
lands).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import server as _server_mod
import verification_loop as _verification_loop
from pause_state import PAUSE_CHECKPOINT_FILENAME, write_pause_checkpoint
from plan_state import PlanState
from shutdown_checkpoint import SHUTDOWN_STOP_REASON


def _naive_local(moment: datetime) -> str:
    """``moment`` (aware) spelled the way the runtime state spells it.

    The verification runtime state writes ``started_at`` as a naive
    *local* stamp while ``pause_state`` writes ``paused_at`` as an aware
    UTC one, so the two are computed rather than hard-coded -- each test
    then means the same instant whatever timezone it runs in.
    """
    return moment.astimezone().replace(tzinfo=None).isoformat()


def _stamps(*, fresh: bool) -> tuple[str, str]:
    """``(started_at, paused_at)`` for a run and the checkpoint that ended it.

    ``fresh=True`` is a checkpoint written *during* the run (the server
    went down under it); ``fresh=False`` is a checkpoint left over from
    an earlier run of the same plan, which says nothing about this one.
    """
    paused = datetime.now(timezone.utc)
    started = paused - timedelta(minutes=1) if fresh else paused + timedelta(minutes=1)
    return _naive_local(started), paused.isoformat()


def _seed_plan(plans_dir, plan_id: str, phase: str):
    """A plan directory whose routing row sits at ``phase``."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    PlanState(plan_dir).force_set_phase(phase)
    return plan_dir


def _write_shutdown_checkpoint(plan_dir, *, paused_at: str) -> dict:
    """Write the checkpoint the shutdown path would have written.

    It goes through the public writer -- which owns the schema version --
    and then rewrites the one field the test needs to pin.
    """
    document = dict(
        write_pause_checkpoint(
            plan_dir, kind="verification", reason=SHUTDOWN_STOP_REASON
        )
    )
    document["paused_at"] = paused_at
    (plan_dir / PAUSE_CHECKPOINT_FILENAME).write_text(
        json.dumps(document), encoding="utf-8"
    )
    return document


def _runtime_state(status: str, started_at: str, *, round_no: int = 3) -> dict:
    """What ``_load_verification_runtime_state`` answers for this plan."""
    return {
        "verification_status": status,
        "verification_round": round_no,
        "verification_max_rounds": 5,
        "results": {},
        "started_at": started_at,
        "updated_at": started_at,
    }


@pytest.fixture()
def recover(monkeypatch):
    """Run the startup scan over a plans directory, in isolation.

    The in-memory registry is swapped for a fresh dict so a recovered
    entry cannot leak into another test, and the runtime-state loader is
    answered from the test rather than from the state database.
    """
    registry: dict = {}
    monkeypatch.setattr(_server_mod, "_verification_state", registry)

    def _run(plans_dir, plan_id=None, runtime=None, runtimes=None) -> dict:
        table = dict(runtimes or {})
        if plan_id is not None:
            table[plan_id] = runtime
        monkeypatch.setattr(
            _server_mod,
            "_load_verification_runtime_state",
            lambda asked: table.get(asked),
        )
        _server_mod._recover_verification_states(plans_dir)
        return registry

    return _run


def test_a_fresh_verification_checkpoint_requeues_the_plan(tmp_path, recover):
    """The server went down under this round: the plan goes back to queued."""
    plan_id = "verify-requeue-fresh"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "verification_running")
    started_at, paused_at = _stamps(fresh=True)
    _write_shutdown_checkpoint(plan_dir, paused_at=paused_at)

    recover(
        plans_dir,
        plan_id,
        _runtime_state("running", started_at),
    )

    assert PlanState(plan_dir).get_current_phase() == "queued"


def test_the_round_is_preserved(tmp_path, recover):
    """Requeueing the plan does not throw away the round it had reached."""
    plan_id = "verify-requeue-round"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "verification_running")
    started_at, paused_at = _stamps(fresh=True)
    _write_shutdown_checkpoint(plan_dir, paused_at=paused_at)

    registry = recover(
        plans_dir,
        plan_id,
        _runtime_state("running", started_at),
    )

    assert plan_id in registry, "the running round is still recovered"
    assert registry[plan_id]["verification_round"] == 3


def test_the_recovered_entry_is_marked_as_resumed_from_shutdown(
    tmp_path, recover
):
    """The recovered entry says *why* it was recovered.

    ``recovered`` says "this entry was rebuilt from disk"; the shutdown
    marker says the stronger thing -- the server went down under this
    round. A crash recovery and a shutdown recovery are different
    events, and the entry is what says which one happened.
    """
    plan_id = "verify-requeue-marked"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "verification_running")
    started_at, paused_at = _stamps(fresh=True)
    _write_shutdown_checkpoint(plan_dir, paused_at=paused_at)

    registry = recover(
        plans_dir,
        plan_id,
        _runtime_state("running", started_at),
    )

    assert registry[plan_id].get("resumed_from_shutdown") is True


def test_a_failing_requeue_does_not_stop_the_recovery(tmp_path, recover, monkeypatch):
    """One plan that cannot be requeued must not cost the others theirs.

    The recovery sweep runs over every plan at startup. A plan whose
    phase cannot be written is a broken plan, not a broken sweep -- the
    failure is logged and the scan moves on.
    """
    broken_id = "verify-requeue-broken"
    healthy_id = "verify-requeue-healthy"
    plans_dir = tmp_path / "plans"
    broken_dir = _seed_plan(plans_dir, broken_id, "verification_running")
    healthy_dir = _seed_plan(plans_dir, healthy_id, "verification_running")
    broken_started, broken_paused = _stamps(fresh=True)
    healthy_started, healthy_paused = _stamps(fresh=True)
    _write_shutdown_checkpoint(broken_dir, paused_at=broken_paused)
    _write_shutdown_checkpoint(healthy_dir, paused_at=healthy_paused)

    real_plan_state = _server_mod.PlanState

    class _ExplodingPlanState:
        """A plan state whose phase write fails for one plan only."""

        def __init__(self, plan_dir):
            self._plan_dir = plan_dir

        def force_set_phase(self, phase):
            if Path(self._plan_dir).name == broken_id:
                raise RuntimeError("this plan's phase cannot be written")
            real_plan_state(self._plan_dir).force_set_phase(phase)

    monkeypatch.setattr(_server_mod, "PlanState", _ExplodingPlanState)

    registry = recover(
        plans_dir,
        runtimes={
            broken_id: _runtime_state("running", broken_started),
            healthy_id: _runtime_state("running", healthy_started),
        },
    )

    assert PlanState(healthy_dir).get_current_phase() == "queued"
    assert healthy_id in registry
    # The broken plan really did fail -- the sweep moved on, it did not
    # quietly requeue everything.
    assert PlanState(broken_dir).get_current_phase() == "verification_running"


def test_no_checkpoint_keeps_the_existing_recovery(tmp_path, recover):
    """No checkpoint at all means "the work died": nothing is requeued."""
    plan_id = "verify-requeue-absent"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "verification_running")
    started_at, _paused_at = _stamps(fresh=True)

    registry = recover(
        plans_dir,
        plan_id,
        _runtime_state("running", started_at),
    )

    assert PlanState(plan_dir).get_current_phase() == "verification_running"
    assert registry[plan_id]["verification_round"] == 3


def test_a_stale_verification_checkpoint_keeps_the_existing_recovery(
    tmp_path, recover
):
    """A checkpoint from an earlier run says nothing about this one."""
    plan_id = "verify-requeue-stale"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "verification_running")
    started_at, paused_at = _stamps(fresh=False)
    _write_shutdown_checkpoint(plan_dir, paused_at=paused_at)

    registry = recover(
        plans_dir,
        plan_id,
        _runtime_state("running", started_at),
    )

    assert PlanState(plan_dir).get_current_phase() == "verification_running"
    assert registry[plan_id]["verification_round"] == 3


def test_a_shutdown_interrupted_repairing_round_is_requeued(tmp_path, recover):
    """``repairing`` is in flight too: it is requeued, but not loaded."""
    plan_id = "verify-requeue-repairing"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "verification_repairing")
    started_at, paused_at = _stamps(fresh=True)
    _write_shutdown_checkpoint(plan_dir, paused_at=paused_at)

    registry = recover(
        plans_dir,
        plan_id,
        _runtime_state("repairing", started_at),
    )

    assert PlanState(plan_dir).get_current_phase() == "queued"
    assert plan_id not in registry, (
        "only a running round is loaded into memory; a repairing round "
        "is requeued and nothing else"
    )


def test_a_shutdown_interrupted_rerunning_round_is_requeued(tmp_path, recover):
    """``rerunning`` is the third in-flight status: it is requeued too.

    The writer side stamps a checkpoint for all three in-flight
    statuses, so the reader side has to recognise all three -- otherwise
    the checkpoints written for the two that are not ``running`` would
    never be consumed.
    """
    plan_id = "verify-requeue-rerunning"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "verification_repairing")
    started_at, paused_at = _stamps(fresh=True)
    _write_shutdown_checkpoint(plan_dir, paused_at=paused_at)

    registry = recover(
        plans_dir,
        plan_id,
        _runtime_state("rerunning", started_at),
    )

    assert PlanState(plan_dir).get_current_phase() == "queued"
    assert plan_id not in registry


def test_a_stale_repairing_checkpoint_keeps_the_existing_recovery(
    tmp_path, recover
):
    """A stale checkpoint leaves a repairing round exactly where it was."""
    plan_id = "verify-requeue-repairing-stale"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "verification_repairing")
    started_at, paused_at = _stamps(fresh=False)
    _write_shutdown_checkpoint(plan_dir, paused_at=paused_at)

    registry = recover(
        plans_dir,
        plan_id,
        _runtime_state("repairing", started_at),
    )

    assert PlanState(plan_dir).get_current_phase() == "verification_repairing"
    assert plan_id not in registry


# ---------------------------------------------------------------------------
# 2026-11-13 (VP-027): the writer that was racing the shutdown
# ---------------------------------------------------------------------------
#
# The recovery scan above answers "the server went down under this
# round" for the three IN-FLIGHT statuses.  There is a fourth shape it
# has to recognise: the round was in flight when the server went down,
# a writer that was still running stamped ``failed`` over it, and the
# plan now carries a terminal verdict for a round nobody finished.
#
# Two things follow from that shape:
#
# * the ``failed`` must not stand — the plan goes back to the queue and
#   the round is verified again;
# * the terminal must not be written at all while the shutdown is
#   unaccounted for — a second writer's "failed" is not the round's
#   verdict, and one round has one verdict.
#
# The tests below pin both halves: the refusal at write time, and the
# recovery of a terminal that was already written.


def _verification_row_status(plan_id: str):
    """The ``plan_verification`` row's status for ``plan_id`` (or None).

    Read through the production connection so the answer is what a
    reader of the plan would see, not what a mock was told to say.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(_server_mod._state_db_path())
    try:
        migrate(conn)
        row = conn.execute(
            "SELECT verification_status FROM plan_verification WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _seed_verification_row(plan_id: str, status: str, started_at: str) -> None:
    """A ``plan_verification`` row in ``status``, with a runtime state.

    The runtime state is what makes the row visible to the recovery
    scan: ``_load_verification_runtime_state`` answers ``None`` for a
    row that carries none, and the scan skips a plan it cannot read.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(_server_mod._state_db_path())
    try:
        migrate(conn)
        VerificationRepository(conn).insert(
            plan_id,
            status,
            round=3,
            max_rounds=5,
            runtime_state={"pending_vps": []},
            results={"status": status, "recorded_by": "_persist_verification_terminal"},
            started_at=started_at,
        )
    finally:
        conn.close()


def _alive_round(monkeypatch, plan_id: str, started_at: str) -> None:
    """The round that is writing now is alive, with its start stamp.

    The stamp is what proves the checkpoint belongs to THIS run: a
    checkpoint left over from an earlier run says nothing about it.
    """
    monkeypatch.setattr(
        _server_mod,
        "_verification_state",
        {plan_id: {"started_at": started_at, "verification_status": "running"}},
    )
    monkeypatch.setattr(
        _server_mod, "_refresh_usage_report", lambda *a, **k: None
    )


def test_a_shutdown_interrupted_failure_goes_back_to_the_queue(
    tmp_path, monkeypatch
):
    """The verdict that was racing the shutdown must not land.

    The server went down under this round, and a writer that was still
    running stamped ``failed`` over it.  That failure is not the round's
    verdict — the round was interrupted — so the plan goes back to the
    queue and no verdict is written at all.
    """
    plan_id = "verify-terminal-interrupted"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "verification_running")
    started_at, paused_at = _stamps(fresh=True)
    _write_shutdown_checkpoint(plan_dir, paused_at=paused_at)
    _seed_verification_row(plan_id, "running", started_at)
    _alive_round(monkeypatch, plan_id, started_at)

    _verification_loop._record_verification_terminal(
        plan_id, "failed", "verification_interrupted_by_shutdown",
    )

    assert PlanState(plan_dir).get_current_phase() == "queued"
    assert _verification_row_status(plan_id) != "failed"


def test_the_round_verdict_writer_refuses_a_shutdown_interrupted_failure(
    tmp_path, monkeypatch
):
    """The repair path's round verdict is a verdict too.

    It writes the round's own outcome, so a round the shutdown
    interrupted must not leave a ``failed`` behind either.
    """
    plan_id = "verify-round-verdict-interrupted"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "verification_repairing")
    started_at, paused_at = _stamps(fresh=True)
    _write_shutdown_checkpoint(plan_dir, paused_at=paused_at)
    _seed_verification_row(plan_id, "running", started_at)
    _alive_round(monkeypatch, plan_id, started_at)

    _verification_loop._record_round_verdict_persist(
        plan_id, "round-token-1", "failed", "verification_interrupted", None,
    )

    assert PlanState(plan_dir).get_current_phase() == "queued"
    assert _verification_row_status(plan_id) != "failed"


def test_the_recovery_scan_requeues_a_plan_the_second_writer_failed(
    tmp_path, monkeypatch
):
    """A ``failed`` the second writer left behind is an interrupted round.

    The checkpoint was written while the round was in flight; the writer
    that was racing the shutdown stamped ``failed`` over it.  The scan
    has to recognise that shape too, and the erroneous terminal has to
    go WITH the requeue — a row that still says ``failed`` is what the
    next boot re-matches (the checkpoint is still fresh), so the plan
    would be requeued again and again, for ever.
    """
    plan_id = "verify-requeue-second-writer"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "failed")
    started_at, paused_at = _stamps(fresh=True)
    _write_shutdown_checkpoint(plan_dir, paused_at=paused_at)
    _seed_verification_row(plan_id, "failed", started_at)

    monkeypatch.setattr(_server_mod, "_verification_state", {})
    _server_mod._recover_verification_states(plans_dir)

    assert PlanState(plan_dir).get_current_phase() == "queued"
    assert _verification_row_status(plan_id) == "pending"


def test_a_failed_plan_without_a_checkpoint_keeps_its_verdict(
    tmp_path, monkeypatch
):
    """No checkpoint means the work died: the verdict stands.

    The scan must not sweep every failed plan back into the queue —
    only the ones the shutdown can be PROVED to have interrupted.
    """
    plan_id = "verify-requeue-failed-absent"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "failed")
    started_at, _paused_at = _stamps(fresh=True)
    _seed_verification_row(plan_id, "failed", started_at)

    monkeypatch.setattr(_server_mod, "_verification_state", {})
    _server_mod._recover_verification_states(plans_dir)

    assert PlanState(plan_dir).get_current_phase() == "failed"
    assert _verification_row_status(plan_id) == "failed"


def test_a_pass_is_not_refused_by_the_shutdown_checkpoint(tmp_path, monkeypatch):
    """The guard is about the FAILURE verdict: a pass still lands.

    A round that finished before the server went down has a verdict
    worth keeping; refusing it would throw away a real result.
    """
    plan_id = "verify-terminal-passed"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "verification_running")
    started_at, paused_at = _stamps(fresh=True)
    _write_shutdown_checkpoint(plan_dir, paused_at=paused_at)
    _seed_verification_row(plan_id, "running", started_at)
    _alive_round(monkeypatch, plan_id, started_at)

    _verification_loop._record_verification_terminal(
        plan_id, "passed", "all_vps_passed",
    )

    assert PlanState(plan_dir).get_current_phase() == "completed"
    assert _verification_row_status(plan_id) == "passed"


# ---------------------------------------------------------------------------
# The recovery path does not write the retired mirror column (2026-11-24)
# ---------------------------------------------------------------------------
#
# ``plan_routing.verification`` was retired as a *state source* by arch
# decision point 3: the column carries the frozen baseline and nothing
# else, and its only writer is
# ``RoutingRepository.write_verification_baseline``.  The shutdown
# recovery used to write it anyway -- it reset the column's ``status``
# / ``stop_reason`` lifecycle keys, which are exactly the two keys the
# column no longer carries.  That made the recovery path a second
# writer of a retired column, and the write would have overwritten the
# frozen baseline with a lifecycle answer.
#
# The static gates catch the *statement*; this test pins the
# *behaviour*, so the recovery cannot be hollowed out to make them
# green: the recovery still has to requeue the plan and still has to
# put the authoritative ``plan_verification`` row back to ``pending``.


def _routing_verification_blob(plan_id: str):
    """The raw ``plan_routing.verification`` column for ``plan_id``.

    Read straight out of the row, so the answer is the bytes on disk
    and not a decoded view of them.  ``None`` when the row is missing.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(_server_mod._state_db_path())
    try:
        migrate(conn)
        row = conn.execute(
            "SELECT verification FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _seed_routing_verification_baseline(plan_id: str) -> None:
    """Put a frozen baseline into the routing row's ``verification`` column.

    It goes through the column's only writer
    (``RoutingRepository.write_verification_baseline``), so the blob is
    the one production would have written.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    conn = open_db(_server_mod._state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).write_verification_baseline(
            plan_id,
            {"verification_points": [{"id": "vp-1", "question": "does it?"}]},
            doc_sha256="0123456789abcdef",
        )
    finally:
        conn.close()


def test_the_recovery_does_not_write_the_retired_mirror_column(
    tmp_path, monkeypatch
):
    """The recovery leaves the frozen baseline alone -- and still recovers.

    Two things have to hold at once, and only together they are worth
    anything:

    * the ``plan_routing.verification`` column is byte for byte what it
      was before the scan -- the recovery writes no mirror, so the
      frozen baseline survives it;
    * the recovery still happens: the plan goes back to ``queued`` and
      the authoritative ``plan_verification`` row goes back to
      ``pending``.

    A recovery that had been hollowed out (no write to
    ``plan_verification`` at all) would leave the mirror untouched too
    -- so the mirror assertion alone proves nothing.  That is why the
    second half is here.
    """
    plan_id = "verify-requeue-mirror-untouched"
    plans_dir = tmp_path / "plans"
    plan_dir = _seed_plan(plans_dir, plan_id, "failed")
    started_at, paused_at = _stamps(fresh=True)
    _write_shutdown_checkpoint(plan_dir, paused_at=paused_at)
    _seed_verification_row(plan_id, "failed", started_at)
    _seed_routing_verification_baseline(plan_id)

    before = _routing_verification_blob(plan_id)
    assert before is not None and before != "", (
        "the routing row has no verification column to compare against -- "
        "the assertion below would be vacuous"
    )
    assert json.loads(before)["baseline"], (
        "the seeded column carries no baseline -- the assertion below "
        "would compare two empty blobs"
    )

    monkeypatch.setattr(_server_mod, "_verification_state", {})
    _server_mod._recover_verification_states(plans_dir)

    after = _routing_verification_blob(plan_id)
    assert after == before, (
        "the recovery wrote the retired plan_routing.verification column:\n"
        f"  before: {before}\n"
        f"  after:  {after}\n"
        "The column carries the frozen baseline and has exactly one "
        "writer (RoutingRepository.write_verification_baseline); the "
        "recovery must not write it."
    )

    # The recovery itself still has to happen.
    assert PlanState(plan_dir).get_current_phase() == "queued"
    assert _verification_row_status(plan_id) == "pending"
