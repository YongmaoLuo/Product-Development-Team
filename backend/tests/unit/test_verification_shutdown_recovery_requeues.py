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
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import server as _server_mod
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
