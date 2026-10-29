"""A shutdown leaves a checkpoint behind for every plan it interrupted.

Background
----------
``server._shutdown_all_executions`` kills the execution and verification
subprocesses and walks away.  Nothing on disk said *what* was running
when the process went down, so a restart found a plan that looked
exactly like a plan that had never run -- the work in flight was lost
silently.

``backend/pause_state.py`` already knows how to write a per-plan
checkpoint (``write_pause_checkpoint``); this file is the *caller* that
the shutdown path needs and does not yet have:
``backend/shutdown_checkpoint.py``.

What these tests pin
--------------------
* **coverage** -- every in-flight plan gets a checkpoint, and a plan
  that was not running does not;
* **shape** -- the checkpoint says ``reason='server_shutdown'``, and it
  carries the execution's ``project_dir`` and the verification's
  ``verification_round``;
* **isolation** -- one plan's failure does not cost the other plans
  their checkpoints, and the whole sweep never raises;
* **budget** -- the sweep gives up rather than delaying the shutdown;
* **idempotence** -- the lifespan shutdown and the atexit hook both
  call the writer, and the second call must not stamp a second
  checkpoint over the first.

The writer is exercised through its public seam only: the states are
plain dicts, the plan directory comes from a caller-supplied callable,
and the on-disk result is read back with
``pause_state.read_pause_checkpoint``.  No server, no subprocess, no
network -- this is the unit lane.

Note that ``verification_state`` entries carry no ``project_dir``: the
runtime state has no such key, and the writer passes ``None`` rather
than inventing a path.
"""

import logging
import sys
import types
from pathlib import Path

import pytest

import shutdown_checkpoint
from pause_state import read_pause_checkpoint
from shutdown_checkpoint import (
    DEFAULT_SHUTDOWN_CHECKPOINT_BUDGET_SECONDS,
    SHUTDOWN_STOP_REASON,
    reset_shutdown_checkpoint_state,
    write_shutdown_checkpoints,
)


@pytest.fixture(autouse=True)
def _isolate_written_plan_ids():
    """The written-plan set is module state; every test starts empty."""
    reset_shutdown_checkpoint_state()
    yield
    reset_shutdown_checkpoint_state()


def _plan_dir_for(root, *plan_ids):
    """A ``plan_dir_for`` callable with the named plan directories present."""
    for plan_id in plan_ids:
        (root / plan_id).mkdir(parents=True, exist_ok=True)
    return lambda plan_id: root / plan_id


def _no_interruption(plan_id, plan_dir):
    """An ``interruption_for`` callable that found nothing to interrupt."""
    return [], []


def test_writer_checkpoints_every_in_flight_execution(tmp_path):
    """Two running executions get a checkpoint; the completed one does not."""
    execution_state = {
        "p1": {"status": "running", "project_dir": "/w/p1"},
        "p2": {"status": "running", "project_dir": "/w/p2"},
        "p3": {"status": "completed", "project_dir": "/w/p3"},
    }

    result = write_shutdown_checkpoints(
        execution_state,
        {},
        plan_dir_for=_plan_dir_for(tmp_path, "p1", "p2", "p3"),
        interruption_for=_no_interruption,
    )

    assert result["written"] == ["p1", "p2"]
    assert result["failed"] == []
    for plan_id in ("p1", "p2"):
        checkpoint = read_pause_checkpoint(tmp_path / plan_id)
        assert checkpoint is not None, plan_id
        assert checkpoint["kind"] == "execution"
        assert checkpoint["reason"] == SHUTDOWN_STOP_REASON
        assert checkpoint["project_dir"] == "/w/" + plan_id
    assert read_pause_checkpoint(tmp_path / "p3") is None


def test_writer_records_verification_round(tmp_path):
    """A verification checkpoint carries the round that was in flight."""
    verification_state = {
        "v1": {"verification_status": "running", "verification_round": 3},
    }

    result = write_shutdown_checkpoints(
        {},
        verification_state,
        plan_dir_for=_plan_dir_for(tmp_path, "v1"),
        interruption_for=_no_interruption,
    )

    assert result["written"] == ["v1"]
    checkpoint = read_pause_checkpoint(tmp_path / "v1")
    assert checkpoint is not None
    assert checkpoint["kind"] == "verification"
    assert checkpoint["verification_round"] == 3
    # The runtime state has no ``project_dir`` key, so the writer must not
    # invent one.
    assert checkpoint["project_dir"] is None


def test_one_failed_write_does_not_stop_the_others(tmp_path, monkeypatch):
    """A plan that fails to be written is reported; the others still land."""
    real_write = shutdown_checkpoint.write_pause_checkpoint

    def flaky_write(plan_dir, **kwargs):
        if Path(plan_dir).name == "p1":
            raise RuntimeError("disk on fire")
        return real_write(plan_dir, **kwargs)

    monkeypatch.setattr(
        shutdown_checkpoint, "write_pause_checkpoint", flaky_write
    )

    result = write_shutdown_checkpoints(
        {
            "p1": {"status": "running", "project_dir": "/w/p1"},
            "p2": {"status": "running", "project_dir": "/w/p2"},
        },
        {},
        plan_dir_for=_plan_dir_for(tmp_path, "p1", "p2"),
        interruption_for=_no_interruption,
    )

    assert result["written"] == ["p2"]
    assert [failure["plan_id"] for failure in result["failed"]] == ["p1"]
    assert read_pause_checkpoint(tmp_path / "p2") is not None


def test_budget_exhaustion_stops_writing_without_raising(tmp_path):
    """Past the budget the sweep stops -- and it never raises."""
    clock = {"now": 0.0}

    def monotonic():
        clock["now"] += 10.0
        return clock["now"]

    result = write_shutdown_checkpoints(
        {"p1": {"status": "running", "project_dir": "/w/p1"}},
        {},
        plan_dir_for=_plan_dir_for(tmp_path, "p1"),
        interruption_for=_no_interruption,
        monotonic=monotonic,
    )

    assert result == {"written": [], "failed": []}
    assert read_pause_checkpoint(tmp_path / "p1") is None


def test_a_plan_written_once_is_not_rewritten(tmp_path, monkeypatch):
    """The lifespan call and the atexit call write one checkpoint, not two."""
    calls = []
    real_write = shutdown_checkpoint.write_pause_checkpoint

    def counting_write(plan_dir, **kwargs):
        calls.append(Path(plan_dir).name)
        return real_write(plan_dir, **kwargs)

    monkeypatch.setattr(
        shutdown_checkpoint, "write_pause_checkpoint", counting_write
    )

    kwargs = {
        "plan_dir_for": _plan_dir_for(tmp_path, "p1"),
        "interruption_for": _no_interruption,
    }
    first = write_shutdown_checkpoints(
        {"p1": {"status": "running", "project_dir": "/w/p1"}}, {}, **kwargs
    )
    second = write_shutdown_checkpoints(
        {"p1": {"status": "running", "project_dir": "/w/p1"}}, {}, **kwargs
    )

    assert first["written"] == ["p1"]
    assert second["written"] == []
    assert calls == ["p1"]


def test_reset_shutdown_checkpoint_state_allows_a_second_write(tmp_path):
    """Resetting the module state makes the same plan writable again."""
    kwargs = {
        "plan_dir_for": _plan_dir_for(tmp_path, "p1"),
        "interruption_for": _no_interruption,
    }
    state = {"p1": {"status": "running", "project_dir": "/w/p1"}}

    assert write_shutdown_checkpoints(state, {}, **kwargs)["written"] == ["p1"]
    assert write_shutdown_checkpoints(state, {}, **kwargs)["written"] == []

    reset_shutdown_checkpoint_state()

    assert write_shutdown_checkpoints(state, {}, **kwargs)["written"] == ["p1"]


def test_interruption_failure_still_writes_the_checkpoint(tmp_path):
    """A broken interruption lookup costs the task lists, not the checkpoint."""
    def broken(plan_id, plan_dir):
        raise RuntimeError("no log for you")

    result = write_shutdown_checkpoints(
        {"p1": {"status": "running", "project_dir": "/w/p1"}},
        {},
        plan_dir_for=_plan_dir_for(tmp_path, "p1"),
        interruption_for=broken,
    )

    assert result["written"] == ["p1"]
    checkpoint = read_pause_checkpoint(tmp_path / "p1")
    assert checkpoint is not None
    assert checkpoint["interrupted_tasks"] == []
    assert checkpoint["unfinished_tasks"] == []


def test_a_missing_plan_dir_is_skipped(tmp_path, caplog):
    """A plan whose directory is gone is skipped, not failed."""
    with caplog.at_level(logging.WARNING, logger="shutdown_checkpoint"):
        result = write_shutdown_checkpoints(
            {"p1": {"status": "running", "project_dir": "/w/p1"}},
            {},
            plan_dir_for=lambda plan_id: tmp_path / plan_id,
            interruption_for=_no_interruption,
        )

    assert result == {"written": [], "failed": []}
    assert read_pause_checkpoint(tmp_path / "p1") is None
    assert any(
        "shutdown_checkpoint_plan_dir_missing" in record.message
        for record in caplog.records
    )


def test_a_failing_plan_dir_lookup_is_reported_not_raised(tmp_path):
    """A plan whose directory cannot be resolved is reported, not raised."""
    (tmp_path / "p2").mkdir(parents=True, exist_ok=True)

    def plan_dir_for(plan_id):
        if plan_id == "p1":
            raise RuntimeError("no such plan")
        return tmp_path / plan_id

    result = write_shutdown_checkpoints(
        {
            "p1": {"status": "running", "project_dir": "/w/p1"},
            "p2": {"status": "running", "project_dir": "/w/p2"},
        },
        {},
        plan_dir_for=plan_dir_for,
        interruption_for=_no_interruption,
    )

    assert result["written"] == ["p2"]
    assert [failure["plan_id"] for failure in result["failed"]] == ["p1"]
    assert read_pause_checkpoint(tmp_path / "p2") is not None


def test_only_in_flight_verification_statuses_are_checkpointed(tmp_path):
    """Only the three in-flight verification statuses are written."""
    verification_state = {
        "v1": {"verification_status": "running", "verification_round": 1},
        "v2": {"verification_status": "repairing", "verification_round": 2},
        "v3": {"verification_status": "rerunning", "verification_round": 3},
        "v4": {"verification_status": "completed", "verification_round": 4},
        "v5": {"verification_status": "paused", "verification_round": 5},
    }

    result = write_shutdown_checkpoints(
        {},
        verification_state,
        plan_dir_for=_plan_dir_for(tmp_path, "v1", "v2", "v3", "v4", "v5"),
        interruption_for=_no_interruption,
    )

    assert result["written"] == ["v1", "v2", "v3"]
    for plan_id in ("v4", "v5"):
        assert read_pause_checkpoint(tmp_path / plan_id) is None


def test_the_default_budget_is_five_seconds():
    """The shipped budget is the one the architecture decision named."""
    assert DEFAULT_SHUTDOWN_CHECKPOINT_BUDGET_SECONDS == 5.0
    assert SHUTDOWN_STOP_REASON == "server_shutdown"


def test_the_default_lookup_calls_stop_interruption_with_hard_killed_false(
    tmp_path, monkeypatch
):
    """The default lookup is the stop helper, called with an explicit flag.

    ``routes.execution._stop_interruption`` takes ``hard_killed`` as a
    keyword-only argument *without* a default, so the writer has to pass
    it. The stub below stands in for the module so the unit lane does
    not have to import the server to check how the writer calls it.
    """
    calls = []

    def fake_stop_interruption(plan_id, plan_dir, *, hard_killed):
        calls.append((plan_id, Path(plan_dir), hard_killed))
        return [{"task_id": "1"}], [{"task_id": "2"}]

    stub = types.ModuleType("routes.execution")
    stub._stop_interruption = fake_stop_interruption
    monkeypatch.setitem(sys.modules, "routes.execution", stub)

    result = write_shutdown_checkpoints(
        {"p1": {"status": "running", "project_dir": "/w/p1"}},
        {},
        plan_dir_for=_plan_dir_for(tmp_path, "p1"),
    )

    assert result["written"] == ["p1"]
    assert calls == [("p1", tmp_path / "p1", False)]
    checkpoint = read_pause_checkpoint(tmp_path / "p1")
    assert checkpoint is not None
    assert [entry["task_id"] for entry in checkpoint["interrupted_tasks"]] == ["1"]
    assert [entry["task_id"] for entry in checkpoint["unfinished_tasks"]] == ["2"]