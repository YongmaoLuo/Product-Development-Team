"""A verification point that is only WAITING must not keep holding a gate.

What changed
------------
The verification fan-out holds two capacity gates -- one per group and one
plan-wide -- around the whole of a verification point, and the point's
dispatch queues for a provider slot inside that. A slot is a lease inside
this process: it is held by an agent that is running right now and handed
back when that agent finishes. So "the gate is taken" and "the point is
working" come apart for exactly as long as the tier stays saturated, and
while they are apart the round reads from the outside as a fully busy fleet
and produces nothing.

The queue itself is right to wait -- the thing it waits on is genuinely on
its way. What was missing is a way to stop *holding* while waiting. So the
queue's own accumulated wait is bounded by ``PROVIDER_SLOT_WAIT_SEC``, and
when it runs out the dispatch raises
:class:`coding_tool.ProviderSlotWaitExpired` -- a signal, not a verdict. The
gate wrapper catches it, the two ``finally`` arms hand both gates back, and
the point goes on the queue again.

What this file pins
-------------------
Most of what is here is reverse control, and that is the point of it.

  * **A slow point is not a queued point.** A point that works for longer
    than the bound is not touched by any of this. A bound wrapped around the
    whole verification point would pass every queue test in this file and
    then kill working points in production, so the case is pinned first and
    pinned in its own right.
  * **The signal is not swallowed on the way up.** Between the dispatch and
    the gate there are two layers whose job is to turn an exception into a
    verdict. Both would have turned "I could not get a slot" into FAILED,
    which is a claim about the work that nothing observed.
  * **A queued point gives the gate back.** Width one, and a second point
    must get in while the first is still between attempts. That is "the
    gates went back" in a form readable from outside; a counter sampled at
    the wrong moment would pass either way.
  * **A queued point is not a failed point**, and **it still finishes.**
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_agent as va  # noqa: E402
from coding_tool import ProviderSlotWaitExpired  # noqa: E402
from verification_agent import (  # noqa: E402
    VerificationAgent,
    _VpCapacityGate,
)


#: How long a "working" verification point takes here, and the bound it
#: outlasts. Keeping the bound this small is what lets the slow case be
#: tested in milliseconds while still being strictly slower than the bound.
WORK_SEC = 0.25
BOUND_SEC = 0.05

#: What each verification point does, per attempt, keyed by id. A step is
#: either ``("queued",)`` -- a dispatch that handed its slot wait back --
#: or ``("work", seconds)`` -- work, which must never be cut. The point
#: cannot tell whether it is being re-queued; it only knows its call
#: produced nothing.
STEPS: dict = {}


def _expired():
    return ProviderSlotWaitExpired(
        "scene 'verification' waited past the slot bound",
        scene="verification",
        waited_sec=BOUND_SEC,
        rounds=1,
        bound_sec=BOUND_SEC,
    )


class _RecordingGate(_VpCapacityGate):
    """The real gate, remembering itself so its depth can be read.

    Substituted through the module name ``_execute_group`` looks the class
    up in, so this is the gate production builds -- not a stand-in with its
    own accounting that could agree with the wrong thing.
    """

    instances: list = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        type(self).instances.append(self)

    @classmethod
    def reset(cls):
        cls.instances = []

    @classmethod
    def depths(cls):
        return [g.in_flight for g in cls.instances]


@pytest.fixture(autouse=True)
def _record_gates(monkeypatch):
    _RecordingGate.reset()
    monkeypatch.setattr(va, "_VpCapacityGate", _RecordingGate)
    yield
    _RecordingGate.reset()


@pytest.fixture(autouse=True)
def _clear_steps():
    STEPS.clear()
    yield
    STEPS.clear()


class _ScriptedAgent(VerificationAgent):
    """A verification point that follows a scripted sequence of steps."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attempts: dict = {}
        self.timeline: list = []

    async def _run_single_vp_async(self, vp: dict) -> dict:
        vp_id = str(vp.get("id", "?"))
        seen = self.attempts.get(vp_id, 0)
        self.attempts[vp_id] = seen + 1
        steps = STEPS.get(vp_id) or []
        step = steps[seen] if seen < len(steps) else ("work", 0.0)

        self.timeline.append((vp_id, seen, "enter"))
        if step[0] == "queued":
            self.timeline.append((vp_id, seen, "queued"))
            raise _expired()
        await asyncio.sleep(step[1])
        self.timeline.append((vp_id, seen, "done"))
        return {"id": vp_id, "status": "PASSED", "reasons": [], "evidence": []}

    def order(self):
        """``[(vp_id, attempt), ...]`` in the order points entered."""
        return [(vp, attempt) for vp, attempt, phase in self.timeline
                if phase == "enter"]


class _DelegatingAgent(VerificationAgent):
    """The real parent path, with the sub-agent boundary replaced.

    ``_run_single_vp_async`` is the production function under test here --
    it is what turns the sub-agent's return into a result dict, and it has a
    defensive ``except Exception`` that would turn the signal into FAILED.
    Replacing only ``_delegate_to_sub_agent`` keeps that function real.
    """

    def __init__(self, *args, delegate, **kwargs):
        super().__init__(*args, **kwargs)
        self._delegate = delegate
        self.attempts = 0

    async def _delegate_to_sub_agent(self, vp, method, vp_id):
        self.attempts += 1
        await self._delegate(vp, method, vp_id)
        return None


@pytest.fixture
def agent_factory(tmp_path):
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    repo = VerificationRepository(conn)
    repo.insert(plan_dir.name, verification_status="not_started")
    conn.commit()

    def _make(width=1, cls=_ScriptedAgent, **kw):
        agent = cls(
            plan_dir=plan_dir,
            project_dir=project_dir,
            coding_tool=None,
            verif_repo=repo,
            **kw,
        )
        # One point at a time, on both layers, so "did the gate come back"
        # is a statement about a gate rather than about arithmetic.
        agent.vp_capacity_resolver = lambda: width
        agent.timeout_policy.parallelism_cap = width
        agent.persistence.start_round(1)
        return agent

    return _make


def _vps(*ids):
    return [{"id": vid, "title": "t", "verification_method": "code_review"}
            for vid in ids]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Reverse control 1: slow, and working, is not stuck
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestASlowPointIsNotAQueuedPoint:
    def test_a_point_that_works_past_the_bound_is_left_alone(
        self, agent_factory, monkeypatch,
    ):
        """The most important test in this file.

        A point that works for longer than the bound is not a queue, and it
        must finish. Every queue test below would still pass if the bound
        were applied to the whole point rather than to the wait, so the
        mis-kill has to be ruled out by its own case instead of being
        assumed away -- it is the failure mode that turns a silent stall into
        a silent kill.
        """
        monkeypatch.setenv("PDT_PROVIDER_SLOT_WAIT_SEC", str(BOUND_SEC))
        STEPS["VP-SLOW"] = [("work", WORK_SEC)]
        agent = agent_factory(width=1)

        loop = asyncio.new_event_loop()
        try:
            started = loop.time()
            results = loop.run_until_complete(
                agent._execute_group("code_review", _vps("VP-SLOW"))
            )
            elapsed = loop.time() - started
        finally:
            loop.close()

        assert elapsed >= WORK_SEC, (
            f"the point took {elapsed:.3f}s; the bound is {BOUND_SEC}s, so "
            f"the only way this passes is if the bound never applied to work"
        )
        assert [r["status"] for r in results] == ["PASSED"], results
        assert all(d == 0 for d in _RecordingGate.depths()), (
            f"a gate was left held after the point finished: "
            f"{_RecordingGate.depths()}"
        )


# ---------------------------------------------------------------------------
# The signal has to survive the layers that turn exceptions into verdicts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTheSignalIsNotSwallowedOnTheWayUp:
    def test_the_sub_agent_reraises_it_instead_of_using_a_retry(
        self, tmp_path,
    ):
        """The self-heal loop's own arm.

        Its ``except Exception`` records a transport error, spends the retry
        budget on it, and -- if the budget runs out -- synthesises a FAILED
        verdict. A verification point that never reached a provider would
        then carry a verdict describing work nobody did.
        """
        from verification_subagent import VerificationSubAgent

        calls = []

        class _QueuedTool:
            def query_json(self, **kwargs):
                calls.append(1)
                raise _expired()

            _last_output_ts = 0.0

        sub = VerificationSubAgent(method="code_review", max_retries=2)

        with pytest.raises(ProviderSlotWaitExpired):
            _run(sub.run(
                {"id": "VP-Q", "title": "t",
                 "verification_method": "code_review",
                 "test_command": "pytest", "expected_result": "pass"},
                coding_tool=_QueuedTool(),
                log_dir=tmp_path / "logs",
            ))

        assert len(calls) == 1, (
            f"the point was dispatched {len(calls)} times; the signal is a "
            f"re-queue, not a malfunction to retry"
        )

    def test_the_parent_verification_point_reraises_it_instead_of_failing(
        self, agent_factory,
    ):
        """The parent's own arm.

        Its ``except Exception`` writes a ``vp_exception`` point-log entry
        and returns ``status="FAILED"`` -- a verdict on the work, produced by
        a provider pool that was merely busy.
        """
        written = []

        async def _delegate(vp, method, vp_id):
            raise _expired()

        agent = agent_factory(
            width=1, cls=_DelegatingAgent, delegate=_delegate,
        )
        agent.persistence.write_verification_point_log = (
            lambda vp_id, event, data=None: written.append((vp_id, event))
        )

        with pytest.raises(ProviderSlotWaitExpired):
            _run(agent._run_single_vp_async(_vps("VP-Q")[0]))

        assert agent.attempts == 1
        assert [e for _v, e in written if e == "vp_exception"] == [], (
            f"a point that could not get a slot was recorded as an "
            f"exception: {written}"
        )


# ---------------------------------------------------------------------------
# Reverse control 2: a queued point gives the gate back
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAQueuedPointGivesTheGateBack:
    def test_a_second_point_gets_in_while_the_first_is_re_queuing(
        self, agent_factory,
    ):
        """The claim, in a form readable from outside.

        Width one on both layers. The first point misses a slot on its first
        two attempts; the second point needs nothing and must therefore be
        running *while the first is still going*. If the gates were still
        held across a re-queue, the second point could not enter until the
        first finished -- and the whole point of the change is that a point
        which is only waiting stops occupying the width.

        This also pins that a re-queued point goes to the back of the queue
        rather than straight back to the front: the gate hands its slot
        straight back when there is room, so a point that returned into it
        without yielding would retake its own slot on every round and starve
        whatever was already queued behind it.
        """
        STEPS["VP-Q"] = [("queued",), ("queued",), ("work", 0.01)]
        STEPS["VP-C"] = [("work", 0.01)]
        agent = agent_factory(width=1)

        _run(agent._execute_group("code_review", _vps("VP-Q", "VP-C")))

        order = agent.order()
        q_first = order.index(("VP-Q", 0))
        c_entry = order.index(("VP-C", 0))
        q_last = order.index(("VP-Q", 2))

        assert q_first < c_entry < q_last, (
            f"the second point ran at position {c_entry}, outside the "
            f"first point's re-queue window ({q_first}..{q_last}): "
            f"{order}. Either the gates were never handed back, or the "
            f"re-queued point took its own slot straight back."
        )

    def test_both_gates_are_empty_when_the_round_ends(
        self, agent_factory,
    ):
        """The depth of both gates, read off the real objects.

        Two gates exist for one point here (the group's and the plan-wide),
        and each has to end at zero. A ``release`` that only covered the
        group layer would leave the plan-wide one held for the rest of the
        round, which is invisible until a second group runs.
        """
        STEPS["VP-Q"] = [("queued",), ("work", 0.01)]
        agent = agent_factory(width=1)

        _run(agent._execute_group("code_review", _vps("VP-Q")))

        assert len(_RecordingGate.instances) == 2, (
            f"expected a group gate and a plan-wide gate, got "
            f"{len(_RecordingGate.instances)}"
        )
        assert _RecordingGate.depths() == [0, 0], (
            f"a gate was still held at the end of the round: "
            f"{_RecordingGate.depths()}"
        )


# ---------------------------------------------------------------------------
# Reverse control 3: a queued point is not a failed point
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAQueuedPointIsNotAFailedPoint:
    def test_a_re_queued_point_ends_as_a_pass_and_no_failure_is_written(
        self, agent_factory,
    ):
        """The whole point of the signal: not a verdict.

        A point that could not get a slot did not do the work badly, and
        nothing observed a result for it. It goes back on the queue, gets a
        slot, and the round records what actually happened.
        """
        written = []
        STEPS["VP-Q"] = [("queued",), ("queued",), ("work", 0.01)]
        agent = agent_factory(width=1)
        agent.persistence.write_verification_point_log = (
            lambda vp_id, event, data=None: written.append((vp_id, event))
        )

        results = _run(
            agent._execute_group("code_review", _vps("VP-Q"))
        )

        assert [r["status"] for r in results] == ["PASSED"], results
        assert agent.attempts["VP-Q"] == 3, agent.attempts
        assert not [e for _v, e in written
                    if e in ("vp_exception", "vp_failed")], written

    def test_a_re_queued_point_produces_one_result_not_one_per_attempt(
        self, agent_factory,
    ):
        """One point in, one result out, however many times it was queued.

        The gate wrapper loops internally, so ``gather`` still sees one
        future per point. That has to stay true: the downstream consumers
        -- the report generator, the orchestrator's ``_extract_failed_ids``
        -- index results against the plan, and a second entry for the same
        point would be a duplicate row in the report rather than a retry.
        """
        STEPS["VP-Q"] = [("queued",), ("queued",), ("work", 0.01)]
        agent = agent_factory(width=1)

        results = _run(agent._execute_group("code_review", _vps("VP-Q")))

        assert len(results) == 1, (
            f"one point in, {len(results)} results out: {results}"
        )
        assert agent.attempts["VP-Q"] == 3, agent.attempts


# ---------------------------------------------------------------------------
# The headline: the round still succeeds end to end
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTheRoundStillCompletes:
    def test_a_point_that_re_queues_and_then_gets_a_slot_passes(
        self, agent_factory,
    ):
        """Main scenario: bound out, gates back, re-queued, and it finishes.

        The signal costs the point its place and nothing else. After the
        re-queue the same point takes a slot, works, and the round records a
        pass for it -- which is the whole contract end to end, from the
        gate down to the result list the report is built from.
        """
        STEPS["VP-Q"] = [("queued",), ("work", 0.01)]
        agent = agent_factory(width=1)

        results = _run(agent._execute_group("code_review", _vps("VP-Q")))

        assert [r["status"] for r in results] == ["PASSED"], results
        assert agent.attempts["VP-Q"] == 2, agent.attempts
        assert all(d == 0 for d in _RecordingGate.depths())
