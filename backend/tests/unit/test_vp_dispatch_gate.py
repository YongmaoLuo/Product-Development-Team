"""2026-11-20: a VP dispatch asks the stop gate before it spends anything.

The verification round's most expensive act is the dispatch: it starts a
sub-agent, it takes a provider slot, it spends the operator's tokens.
Two facts have to be true before one goes out:

  * the plan is **not stopping** — a stop was requested, and a
    dispatch that goes out after it is work nobody is going to read;
  * the dispatch is **counted** — the quiet window's verdict is "nothing
    new was dispatched", and that is only answerable against a count.

So the gate sits at the top of the round's own dispatcher — before
anything is spawned — and the count is taken at the moment the round
commits to the dispatch.  The automatic re-run of the points the
environment blocked is a dispatch too, with the same money, so it asks
the same gate: no side door.

The contracts pinned here:

  1. A dispatch for a stopping plan is **refused** — it raises
     ``VerificationCancelled``, it spawns nothing, and it is not counted.
  2. A dispatch that goes out is **counted** — two verification points,
     two more on the plan's ledger.
  3. A refused dispatch leaves **no mark** behind: no count on the
     ledger, no verdict in anybody's result table.
  4. The re-dispatch path is gated exactly like the first attempt, even
     when the runner it reaches carries no gate of its own.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import stop_state
import verification_cancel
from verification_cancel import VerificationCancelled


@pytest.fixture(autouse=True)
def _clean_registries():
    """Both registries are process-global — a leftover record would leak
    into the next test and make a refusal look like a dispatch."""
    stop_state._reset_for_tests()
    verification_cancel._reset_for_tests()
    yield
    stop_state._reset_for_tests()
    verification_cancel._reset_for_tests()


# ---------------------------------------------------------------------------
# The round, assembled
# ---------------------------------------------------------------------------


def _make_agent(tmp_path: Path):
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=None,
    )


def _vp(vp_id: str) -> dict:
    return {
        "id": vp_id,
        "title": f"title of {vp_id}",
        "verification_method": "code_review",
    }


def _plan_data(*vp_ids: str) -> dict:
    return {"verification_points": [_vp(vp_id) for vp_id in vp_ids]}


def _dispatcher(agent, plan_data: dict):
    """The round's own dispatcher, as the executor drives it.

    This is the production path: ``_build_verification_executor`` wires
    the round's dispatcher into the executor, and the executor calls it
    once per verification point.
    """
    executor = agent._build_verification_executor(plan_data)
    return executor.sub_agent_runner


def _stub_sub_agent(agent, spawned: list):
    """Replace the LLM dispatch with a recorder.

    The sub-agent *is* the spawn; the test only needs to know whether it
    happened.
    """

    async def _fake(vp: dict) -> dict:
        spawned.append(str(vp.get("id")))
        return {
            "id": vp.get("id"),
            "status": "PASSED",
            "reasons": ["stubbed"],
            "evidence": {},
        }

    agent._run_single_vp_async = _fake


def _stop(plan_id: str) -> None:
    """The operator stops the plan from the verification phase."""
    stop_state.begin(
        plan_id,
        scope="verification",
        previous_phase="verification_running",
        dispatch_baseline=0,
    )


# ---------------------------------------------------------------------------
# 1. A dispatch for a stopping plan is refused
# ---------------------------------------------------------------------------


def test_vp_dispatch_blocked_when_stopping(tmp_path: Path):
    """The gate sits in front of the dispatch, not behind it.

    The plan is stopping, so the verification point must not be sent
    anywhere: no sub-agent, no provider slot, no tokens.  The refusal
    travels as ``VerificationCancelled`` — the type every layer above a
    dispatch already treats as "the operator stopped this" rather than
    "this broke".
    """
    agent = _make_agent(tmp_path)
    plan_id = agent.plan_dir.name
    spawned: list = []
    _stub_sub_agent(agent, spawned)
    runner = _dispatcher(agent, _plan_data("VP-007"))

    _stop(plan_id)

    with pytest.raises(VerificationCancelled) as refused:
        asyncio.run(runner(_vp("VP-007")))

    assert plan_id in str(refused.value), refused.value
    assert spawned == [], (
        "a refused dispatch must not spawn a sub-agent — the operator "
        "asked for quiet, not for one more sub-agent"
    )


# ---------------------------------------------------------------------------
# 2. A dispatch that goes out is counted
# ---------------------------------------------------------------------------


def test_note_dispatch_called_per_vp(tmp_path: Path):
    """Two verification points, two dispatches, two on the ledger.

    The count is what the quiet window compares against the baseline the
    stop took, so it has to move once per dispatch — not once per round,
    and not not at all.
    """
    agent = _make_agent(tmp_path)
    plan_id = agent.plan_dir.name
    spawned: list = []
    _stub_sub_agent(agent, spawned)
    runner = _dispatcher(agent, _plan_data("VP-001", "VP-002"))

    asyncio.run(runner(_vp("VP-001")))
    asyncio.run(runner(_vp("VP-002")))

    assert spawned == ["VP-001", "VP-002"]
    assert verification_cancel.dispatch_total(plan_id) == 2


# ---------------------------------------------------------------------------
# 3. A refused dispatch leaves no mark
# ---------------------------------------------------------------------------


def test_blocked_dispatch_not_counted(tmp_path: Path):
    """The refusal costs the ledger nothing — and skews it nothing.

    A refused dispatch is not a dispatch: it must not be counted, or the
    quiet window would read "something was dispatched" for work that
    never went out.  And the count must still be exact afterwards: the
    next dispatch that *does* go out is worth exactly one.
    """
    agent = _make_agent(tmp_path)
    plan_id = agent.plan_dir.name
    spawned: list = []
    _stub_sub_agent(agent, spawned)
    runner = _dispatcher(agent, _plan_data("VP-007", "VP-008"))

    _stop(plan_id)

    with pytest.raises(VerificationCancelled):
        asyncio.run(runner(_vp("VP-007")))

    assert verification_cancel.dispatch_total(plan_id) == 0, (
        "a refused dispatch must not be counted — the quiet window "
        "compares this number with the baseline the stop took"
    )

    # The stop is lifted (a fresh start clears it) — the next dispatch
    # is worth exactly one, so the refusal left no mark on the counter.
    stop_state.clear(plan_id)
    asyncio.run(runner(_vp("VP-008")))

    assert verification_cancel.dispatch_total(plan_id) == 1
    assert spawned == ["VP-008"]


# ---------------------------------------------------------------------------
# 4. The re-dispatch path is gated too
# ---------------------------------------------------------------------------


def test_redispatch_path_is_gated(tmp_path: Path):
    """The automatic re-run is a dispatch with the same money.

    The round re-runs the points the environment blocked, through the
    round's own dispatcher.  That path must ask the same gate — and it
    must ask it *itself*: the runner it reaches is not allowed to be the
    only thing standing between a stopping plan and a fresh sub-agent.
    """
    agent = _make_agent(tmp_path)
    plan_id = agent.plan_dir.name
    spawned: list = []

    async def _ungated_runner(vp: dict) -> dict:
        spawned.append(str(vp.get("id")))
        return {"id": vp.get("id"), "status": "PASSED", "reasons": [], "evidence": {}}

    agent._vp_dispatch_runner = _ungated_runner
    agent._executor_vps_by_id = {"VP-007": _vp("VP-007")}

    _stop(plan_id)

    with pytest.raises(VerificationCancelled):
        agent._dispatch_vps_again(["VP-007"])

    assert spawned == [], (
        "the re-dispatch path is a side door unless it asks the gate "
        "itself — a stopping plan must not get a second round of work"
    )
    assert verification_cancel.dispatch_total(plan_id) == 0


def test_redispatch_refusal_is_not_written_as_failed(tmp_path: Path):
    """A refusal that reaches the re-dispatch keeps its own shape.

    The re-dispatch asks the gate itself, but the runner it reaches may
    refuse as well — and that refusal has to travel on as a refusal.
    Turned into a FAILED verdict it would put "this verification point
    failed" into the round's results table, which is a different claim:
    the point was never dispatched, so nothing about it failed.
    """
    agent = _make_agent(tmp_path)
    plan_id = agent.plan_dir.name

    async def _refusing_runner(vp: dict) -> dict:
        raise VerificationCancelled(plan_id, "the plan is stopping")

    agent._vp_dispatch_runner = _refusing_runner
    agent._executor_vps_by_id = {"VP-007": _vp("VP-007")}

    with pytest.raises(VerificationCancelled):
        agent._dispatch_vps_again(["VP-007"])


# ---------------------------------------------------------------------------
# 5. The children of a split are dispatches too
# ---------------------------------------------------------------------------


def _child(vp_id: str) -> dict:
    return {
        "id": vp_id,
        "title": f"title of {vp_id}",
        "verification_method": "code_review",
        "parent_vp_id": "VP-007",
    }


def test_split_child_dispatch_is_gated(tmp_path: Path):
    """A split's children must not slip past the gate.

    A timed-out verification point is decomposed into clause-level
    sub-points, and every one of them starts its own sub-agent.  The
    parent's gate was asked *before* the split, so a stop that lands
    while the split is being decided would still let the children go
    out — unless the children ask the gate themselves.
    """
    agent = _make_agent(tmp_path)
    plan_id = agent.plan_dir.name
    spawned: list = []
    _stub_sub_agent(agent, spawned)

    _stop(plan_id)

    with pytest.raises(VerificationCancelled):
        asyncio.run(
            agent._run_split_children(_vp("VP-007"), [_child("VP-007-1")]),
        )

    assert spawned == [], (
        "a split's children are dispatches — a stopping plan must not "
        "get a second wave of sub-agents through the split path"
    )
    assert verification_cancel.dispatch_total(plan_id) == 0


def test_split_child_dispatch_is_counted(tmp_path: Path):
    """Every child that goes out is a dispatched verification point.

    The ledger answers "was anything dispatched while the window was
    open", and a child is a verification point with its own id — so it
    is counted like any other.
    """
    agent = _make_agent(tmp_path)
    plan_id = agent.plan_dir.name
    spawned: list = []
    _stub_sub_agent(agent, spawned)

    asyncio.run(
        agent._run_split_children(
            _vp("VP-007"), [_child("VP-007-1"), _child("VP-007-2")],
        ),
    )

    assert sorted(spawned) == ["VP-007-1", "VP-007-2"]
    assert verification_cancel.dispatch_total(plan_id) == 2


# ---------------------------------------------------------------------------
# 6. A dispatch with no plan is neither gated nor counted
# ---------------------------------------------------------------------------


def test_empty_plan_id_is_not_recorded(tmp_path: Path, monkeypatch):
    """An empty plan id is not a plan.

    There is nothing to stop it by — it must not be gated by somebody
    else's stop — and nothing to compare its count against, so it must
    not be written into any ledger.
    """
    agent = _make_agent(tmp_path)
    spawned: list = []
    _stub_sub_agent(agent, spawned)
    runner = _dispatcher(agent, _plan_data("VP-001"))
    monkeypatch.setattr(agent, "_vp_dispatch_plan_id", lambda: "")

    asyncio.run(runner(_vp("VP-001")))

    assert spawned == ["VP-001"], "a dispatch with no plan is not gated"
    assert verification_cancel.dispatch_total("") == 0
