"""A plan that is being stopped must not get a repair executor (2026-11-20).

Why this file exists
--------------------
The repair executor is spawned by the auto-loop **directly**
(:func:`verification_loop._run_repair_execution_async`), so it never
passed through the dispatch channel every other plan-scoped dispatch
goes through — and the stop could not reach it.  A dispatch that is not
gated by the stop is a dispatch the stop cannot kill: the operator asks
for the plan to stop, the round answers by starting a *fresh*
subprocess for that same plan, and the stop is now watching a process
that was born after it.

So the repair dispatch reads the plan-level stop record
(:mod:`stop_state`) and refuses to spawn when the plan is stopping.

Two properties matter, and they are different:

* the **refusal** — nothing is spawned, so the stop stays meaningful;
* the **silence** — the refusal writes nothing.  No execution record
  for the plan, no phase stamp, no terminal verdict.  A plan the
  operator stopped on purpose must not be written down as a failed one,
  and the watchdog must not be handed an entry for a subprocess that
  was never started.

The refusal is raised as :class:`verification_cancel.VerificationCancelled`
because that is the type the auto-loop already treats as "the operator
stopped this" rather than "this broke"; its handler records no
terminal verdict.

A dispatch with **no plan** is not a plan's work, and it is not gated:
``stop_state`` refuses to register an empty plan id at all, so there is
nothing for the gate to find.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import server  # noqa: E402,F401  — imported first: server wires verification_loop
import stop_state  # noqa: E402
import verification_loop  # noqa: E402
from verification_cancel import VerificationCancelled  # noqa: E402

PLAN_ID = "p-repair-spawn-gate"


class _SpawnReached(Exception):
    """Raised by the stubbed spawn helper — it means the gate let it through.

    It is deliberately NOT an ``OSError``: the dispatch catches that one
    and turns it into a "spawn failed" record, which would hide the very
    thing the test is asking about.
    """


@pytest.fixture(autouse=True)
def _clean_registries():
    """Both registries are process-global; every test starts empty."""
    stop_state._reset_for_tests()
    server._execution_state.pop(PLAN_ID, None)
    server._execution_state.pop("", None)
    yield
    stop_state._reset_for_tests()
    server._execution_state.pop(PLAN_ID, None)
    server._execution_state.pop("", None)


@pytest.fixture
def spawn_log(monkeypatch, tmp_path):
    """Stub the two things the dispatch touches on its way to the spawn.

    Returns the list the spawn helper appends to.  It stays empty when
    the gate refused the dispatch, and it holds one entry when the
    dispatch got as far as spawning.
    """
    spawned: list[dict] = []

    def _fake_spawn(**kwargs):
        spawned.append(kwargs)
        raise _SpawnReached("the repair executor was spawned")

    monkeypatch.setattr(server, "_spawn_executor_subprocess", _fake_spawn)
    monkeypatch.setattr(server, "_plan_dir", lambda plan_id: tmp_path / "plan")
    return spawned


def _begin_the_stop(plan_id: str = PLAN_ID) -> None:
    """Record that ``plan_id`` is being stopped, from the repair phase."""
    stop_state.begin(
        plan_id,
        scope=stop_state.SCOPE_REPAIR,
        previous_phase="verification_repairing",
        dispatch_baseline=0,
    )


def test_repair_spawn_blocked_when_stopping(tmp_path, spawn_log):
    """A stopping plan gets no repair executor, and no trace of the refusal.

    The dispatch refuses BEFORE it spawns and BEFORE it writes anything:
    no execution record for the plan, no subprocess, no phase stamp.
    """
    _begin_the_stop()

    with pytest.raises(VerificationCancelled):
        verification_loop._run_repair_execution_async(PLAN_ID, tmp_path)

    assert spawn_log == [], (
        "a stopping plan must not spawn a repair executor: "
        f"{spawn_log!r}"
    )
    assert PLAN_ID not in server._execution_state, (
        "a refused dispatch must not leave an execution record behind — "
        "the watchdog would read it as a run that is going on"
    )


def test_empty_plan_id_is_not_gated(tmp_path, spawn_log):
    """A dispatch with no plan attached is not a plan's work.

    ``stop_state`` refuses to register an empty plan id, so the gate has
    nothing to find and the dispatch proceeds — the negative control for
    "the gate refuses *stopping* plans", not "the gate refuses
    everything".
    """
    _begin_the_stop()

    with pytest.raises(_SpawnReached):
        verification_loop._run_repair_execution_async("", tmp_path)

    assert len(spawn_log) == 1, (
        "an empty plan id must not be gated — only a plan that is "
        f"stopping is: {spawn_log!r}"
    )
