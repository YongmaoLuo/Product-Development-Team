"""The round's VP width is derived from the live fleet, on the path that runs.

What changed
------------
A verification round used to be handed its width as an ``int``:
``run_full_verification`` passed ``self.max_parallel`` down to
``VerificationExecutor.run`` -> ``BaseExecutor.run``, which built one
``asyncio.Semaphore`` of that size and held it for the whole run. An
``int`` is decided once, by a caller, and every caller of the same plan
should have decided the same thing. Two of them did not:

  * ``verification_loop`` :3877 (the auto loop's first round) resolved
    ``_resolve_max_parallel(coding_tool)`` -> 5;
  * ``verification_loop`` :4569 (the post-repair re-entry) passed
    nothing and ate the class default of ``1``.

So a plan ran its repair round one VP at a time, on the same machine,
against the same providers, 87ms after its own first round finished at
five. Nothing about the repair round's work was smaller -- only the
number that reached it was, and it was the wrong number by construction:
the fleet behind it had not changed, so the semaphore was describing
capacity that did not exist.

What this file pins
-------------------
Everything here enters through a **production entry** --
``VerificationAgent.run_full_verification`` or
``VerificationExecutor.run`` -- and measures the thing an operator would
see: **how many verification points were inside the runner at the same
moment**. Not a helper's return value, not a gate's internal counter,
not a width passed in by the test.

  * **The width is derived, not typed.** Pin the scene's capacity at 7
    and 7 VPs run at once -- not 5 (the old default) and not 1 (the old
    re-entry default).
  * **Round 1 and the post-repair round are the same width.** This is
    the bug. Both orchestrators are built by real production code
    (``_run_auto_verification_loop_inner``, first round and
    ``_on_repair_complete``), and their measured widths are compared.
  * **``parallelism_cap`` is the fallback, not the ceiling.** With the
    scene resolvable the derived width wins even when ``parallelism_cap``
    says something smaller; with the scene unresolvable the declared cap
    is what runs.
  * **``None`` is not ``0``.** "Nothing derivable" must not become a gate
    that admits nobody -- and, read the other way, a real ``0`` (every
    provider shut) must not be laundered into "unbounded".
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server as srv  # noqa: E402
import verification_loop as vl  # noqa: E402
import verification_agent as va  # noqa: E402
from verification_agent import (  # noqa: E402
    VerificationAgent,
    _VpCapacityGate,
)


#: Long enough that every admitted VP is still inside the runner when the
#: next batch is admitted, so the peak is the width and not a scheduling
#: artefact. Short enough that the whole file stays in the seconds.
VP_SEC = 0.05


class _ConcurrencyProbe:
    """How many verification points were inside the runner at once.

    Sampled on entry and exit of the runner itself, so it measures the
    thing the gate is supposed to bound. A gate that admits W points at
    a time shows a peak of W; a gate of the wrong width shows that wrong
    width, which is what makes this a falsifier rather than a smoke test.
    """

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0
        self.entered: list = []

    def enter(self, vp_id: str) -> None:
        self.current += 1
        self.entered.append(vp_id)
        self.peak = max(self.peak, self.current)

    def leave(self) -> None:
        self.current -= 1


class _ProbeAgent(VerificationAgent):
    """The production agent with the sub-agent boundary replaced.

    ``_run_single_vp_async`` is where a verification point's work actually
    happens, and it is the last thing before the gate's ``release``.
    Replacing only it leaves real: ``run_full_verification``,
    ``_build_verification_executor``, ``_build_vp_capacity_gate``,
    ``VerificationExecutor.run``, ``BaseExecutor.run`` and the gate.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.probe = _ConcurrencyProbe()

    async def _run_single_vp_async(self, vp: dict) -> dict:
        vp_id = str(vp.get("id", "?"))
        self.probe.enter(vp_id)
        try:
            await asyncio.sleep(VP_SEC)
            return {
                "id": vp_id, "status": "PASSED",
                "reasons": [], "evidence": [],
            }
        finally:
            self.probe.leave()


def _plan(count: int) -> dict:
    """A plan of ``count`` independent VPs, all in layer 0."""
    return {
        "verification_points": [
            {
                "id": "VP-%03d" % i,
                "title": "point %d" % i,
                "verification_method": "code_review",
            }
            for i in range(1, count + 1)
        ]
    }


def _fresh():
    from binary_freshness import FreshnessReport
    # ``is_stale`` is a property (``status == "FAILED"``), not a field,
    # so SKIPPED is the "nothing to check" answer the runner wants.
    return FreshnessReport(kind="none", status="SKIPPED")


def _stub_llm_edges(monkeypatch):
    """Stub only the LLM-shaped edges of the per-VP runner closure.

    The binary-freshness pre-check and the evidence check both make
    claims about a verification point's *verdict*, which is a different
    question from how many run at once. Everything between the plan and
    the gate stays production code.
    """
    monkeypatch.setattr(va, "check_binary_freshness", lambda *a, **k: _fresh())
    monkeypatch.setattr(
        VerificationAgent, "_apply_evidence_check",
        lambda self, vp, verdict: verdict,
    )
    monkeypatch.setattr(
        VerificationAgent, "_attach_evidence_command",
        lambda self, vp, verdict: verdict,
    )


@pytest.fixture
def agent_factory(tmp_path, monkeypatch):
    """A real agent wired to a temp plan + a real (temp) SQLite repo."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    conn = open_db(tmp_path / "state.db")
    migrate(conn)

    _stub_llm_edges(monkeypatch)

    # A fresh plan dir per agent: ``BaseExecutor.run`` filters out VPs that
    # already carry a terminal verdict, so two agents sharing one dir would
    # have the second round run nothing and report a peak of 0 — a width
    # test must measure a width, not an empty round.
    counter = {"n": 0}

    def _make(width=7, cap=4, cls=_ProbeAgent):
        counter["n"] += 1
        plan_dir = tmp_path / ("plan-%d" % counter["n"])
        plan_dir.mkdir()
        repo = VerificationRepository(conn)
        repo.insert(plan_dir.name, verification_status="not_started")
        conn.commit()
        agent = cls(
            plan_dir=plan_dir,
            project_dir=project_dir,
            coding_tool=None,
            verif_repo=repo,
        )
        agent.vp_capacity_resolver = lambda: width
        agent.timeout_policy.parallelism_cap = cap
        agent.persistence.start_round(1)
        return agent

    return _make


def _run_round(agent, count, round_number=1, resume=False):
    """Drive the real ``run_full_verification`` over ``count`` VPs."""
    agent.generate_verification_plan = lambda: _plan(count)
    agent._preflight_service_freshness = lambda plan: None
    agent._resolve_service_placeholders = lambda plan: plan
    agent.generate_verification_report = lambda results: {
        "overall_status": "PASSED", "verification_results": [],
    }
    return agent.run_full_verification(
        round_number=round_number, resume=resume,
    )


# ---------------------------------------------------------------------------
# 1. The width is derived from the scene, not typed by a caller
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTheWidthIsDerived:
    def test_a_pinned_capacity_of_seven_runs_seven_at_once(self, agent_factory):
        """7 in, 7 at once -- from ``run_full_verification``, no width arg.

        Neither old value would pass this: the auto loop's 5 caps it at
        five, and the re-entry path's 1 would serialise the whole plan.
        """
        agent = agent_factory(width=7, cap=4)

        _run_round(agent, 16)

        assert len(agent.probe.entered) == 16, (
            "only %d of 16 VPs ran: %s"
            % (len(agent.probe.entered), agent.probe.entered)
        )
        assert agent.probe.peak == 7, (
            "the scene says 7 VPs can be served but %d ran at once. A "
            "width that is not the derived one is a fixed cap wearing the "
            "gate's name." % agent.probe.peak
        )

    def test_two_different_capacities_give_two_different_widths(
        self, agent_factory,
    ):
        """The number tracks the fleet rather than coinciding with it once.

        A single 7 would also pass if the gate were ignoring its reader
        and some other number happened to be 7. Two pins that differ
        cannot both be a coincidence.
        """
        for width in (3, 9):
            agent = agent_factory(width=width, cap=4)
            _run_round(agent, 16)
            assert agent.probe.peak == width, (
                "pinned capacity %d, measured peak %d"
                % (width, agent.probe.peak)
            )


# ---------------------------------------------------------------------------
# 2. First round == post-repair re-entry round
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTheRepairRoundRunsAsWideAsTheFirst:
    def test_the_reentered_round_measures_the_same_width(
        self, monkeypatch, tmp_path,
    ):
        """The bug, measured on both of the two rounds that disagreed.

        Round 1 is driven through ``_run_auto_verification_loop_inner``,
        and the second through ``_on_repair_complete`` -- the callback
        that builds a *fresh* orchestrator when a repair finishes and
        re-enters verification. Both orchestrators are the real class;
        only the sub-agent boundary is replaced, on the agent class, so
        both rounds measure through the same instrument.
        """
        peaks = {}
        current = {"round": None}
        live = {"n": 0, "peak": 0}

        async def _probed_run_single_vp(self, vp):
            live["n"] += 1
            live["peak"] = max(live["peak"], live["n"])
            try:
                await asyncio.sleep(VP_SEC)
                # FAILED, not PASSED, on purpose: the re-entry runs with
                # ``resume=True``, and ``BaseExecutor.run`` skips VPs that
                # already carry a terminal *success*. A green round 1
                # therefore leaves round 2 nothing to run, which would
                # measure "no round 2" rather than "how wide was round 2".
                return {
                    "id": str(vp.get("id")), "status": "FAILED",
                    "reasons": ["probe"], "evidence": [],
                }
            finally:
                live["n"] -= 1
                if live["n"] == 0:
                    rnd = current["round"]
                    peaks[rnd] = max(peaks.get(rnd, 0), live["peak"])
                    live["peak"] = 0

        _stub_llm_edges(monkeypatch)
        monkeypatch.setattr(
            VerificationAgent, "_run_single_vp_async",
            _probed_run_single_vp,
        )

        plan_id = "plan-reenter"
        plan_dir = tmp_path / plan_id
        plan_dir.mkdir()
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        _wire_loop_fakes(
            monkeypatch, tmp_path, plan_id, plan_dir, current,
        )

        current["round"] = 1
        outcome = vl._run_auto_verification_loop_inner(
            plan_id, plan_dir, project_dir, max_rounds=2, start_round=1,
        )

        assert peaks.get(1), "round 1 never ran a VP: %s (%s)" % (peaks, outcome)
        assert peaks.get(2), (
            "the post-repair round never ran a VP -- the re-entry path was "
            "not exercised at all: %s (%s)" % (peaks, outcome)
        )
        assert peaks[1] == peaks[2], (
            "round 1 ran %d VPs at once and the post-repair round ran %d. "
            "Same machine, same providers, same plan -- a difference here "
            "can only be a number that reached one round and not the other."
            % (peaks[1], peaks[2])
        )
        assert peaks[1] != 1, (
            "the re-entry round collapsed to one at a time: that is the "
            "class default leaking into production again"
        )


def _wire_loop_fakes(monkeypatch, tmp_path, plan_id, plan_dir, current):
    """Enough of ``_run_auto_verification_loop_inner``'s world to re-enter.

    The loop itself is production code and is not touched. What is
    replaced is everything that would reach the outside world: the coding
    tool, the state database (a temp one -- never ``.pdt/state.db``), the
    repair *execution* (which would spawn a subprocess), and the two
    bookkeeping helpers that ask what the repair path left behind.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    repo = VerificationRepository(conn)
    repo.insert(plan_id, verification_status="verifying", max_rounds=2)
    conn.commit()

    # Put the plan in the phase the auto loop's own entry expects, so the
    # loop's routing CAS and the orchestrator's state transitions have the
    # starting point production gives them.
    # ``executing`` first, then ``begin_verification()``: that is the
    # production order, and it is what puts ``execution`` into
    # ``completed_phases`` -- without which the loop's
    # ``transition_to("verification_running")`` refuses to fire.
    from plan_state import PlanState
    ps = PlanState(plan_dir)
    ps.force_set_phase("executing")
    ps.begin_verification()
    ps.set_verification_max_rounds(2)

    monkeypatch.setattr(srv, "_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(srv, "_open_verification_state", lambda: (conn, conn, repo))
    monkeypatch.setattr(srv, "create_coding_tool", lambda *a, **k: _FakeCodingTool())
    monkeypatch.setattr(srv, "_verification_event_logger", lambda plan: None)
    srv._verification_state.setdefault(plan_id, {})

    monkeypatch.setattr(
        VerificationAgent, "generate_verification_plan",
        lambda self: _plan(12),
    )
    monkeypatch.setattr(
        VerificationAgent, "_preflight_service_freshness",
        lambda self, plan: None,
    )
    monkeypatch.setattr(
        VerificationAgent, "_resolve_service_placeholders",
        lambda self, plan: plan,
    )
    monkeypatch.setattr(
        VerificationAgent, "generate_verification_report",
        lambda self, results: {
            "overall_status": "FAILED", "verification_results": [],
        },
    )
    # The repair *decision* is LLM-shaped -- "which VPs failed, what
    # should be tried" -- and has nothing to do with how wide a round runs.
    # Seaming it here keeps the loop, the orchestrator, the agent, the
    # executor and the gate real while making the round fail reliably, so
    # the loop always asks for a repair and round 2 therefore comes from
    # ``_on_repair_complete`` rather than from the round iterator.
    from verification.orchestrator import VerificationOrchestrator

    def _failed_with_repair(self, report, round_number=None):
        """Stand in for "the round failed and the plan was routed to repair".

        The real ``check_cycle_conditions`` does two things: it walks the
        plan into ``verification_repairing`` (a state-machine edge, and
        the repair executor refuses to start without it) and it asks an
        LLM what to repair. Only the second is LLM-shaped, but
        reproducing it means standing up a repair prompt, so both are
        collapsed here -- with the phase edge kept, because without it the
        repair executor declines to run and there is no round 2 to
        measure at all.
        """
        self._safe_phase_call(
            "start_verification_repair", "verification_repairing",
        )
        self._current_repair_tasks = [
            {"task_id": "RP-001", "title": "fix it", "failed_vp_id": "VP-001"},
        ]
        return {
            "status": "failed",
            "stop_reason": "verification_failed",
            "repair_tasks": self._current_repair_tasks,
            "verification_results": [],
            "vp_splits": [],
        }

    monkeypatch.setattr(
        VerificationOrchestrator, "check_cycle_conditions",
        _failed_with_repair,
    )

    # Every round "fails", so the loop always asks for a repair -- which is
    # what makes round 2 come from ``_on_repair_complete`` rather than
    # from the round iterator.
    monkeypatch.setattr(vl, "_repair_path_stop_reason", lambda pid: None)
    monkeypatch.setattr(vl, "_get_pending_repair_tasks", lambda pid, failed=None: [])
    monkeypatch.setattr(
        vl, "_mark_verification_round_running",
        lambda pid, n, **kw: None,
    )
    def _repair_finishes_now(pid, pdir, tool=None, on_complete=None,
                             outcome=None):
        """Stand in for the repair executor subprocess exiting cleanly.

        Synchronous on purpose: the real one spawns a background process
        and calls ``on_complete`` from its exit thread, which would make
        the test's round-2 measurement race the assertion.
        """
        on_complete(0)
        return {"status": "started", "pid": 0}

    monkeypatch.setattr(srv, "_run_repair_execution_async", _repair_finishes_now)

    import verification_cancel
    monkeypatch.setattr(verification_cancel, "is_cancelled", lambda pid: False)

    # Mark round 2 in the probe so the second round's width is recorded
    # separately from the first's.
    real_start = VerificationAgent.run_full_verification

    def _tag_round(self, round_number=1, resume=False):
        current["round"] = round_number
        return real_start(self, round_number=round_number, resume=resume)

    monkeypatch.setattr(VerificationAgent, "run_full_verification", _tag_round)


class _FakeCodingTool:
    provider_priority = ["MiniMax"]

    def query(self, *a, **k):
        raise AssertionError("the loop must not reach the LLM")


# ---------------------------------------------------------------------------
# 3. parallelism_cap is the fallback, not the ceiling
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParallelismCapIsTheFallback:
    def test_a_resolvable_scene_uses_the_derived_width_not_the_cap(
        self, agent_factory,
    ):
        """The cap says 2, the fleet says 9 -- the fleet wins.

        The order is the claim. ``parallelism_cap`` is a *fallback*, and a
        fallback that is consulted first is a ceiling wearing a fallback's
        name.
        """
        agent = agent_factory(width=9, cap=2)

        _run_round(agent, 16)

        assert agent.probe.peak == 9, (
            "measured %d at once with a resolvable scene of 9 and "
            "parallelism_cap=2. The cap is overruleable and the derived "
            "width is not." % agent.probe.peak
        )

    def test_an_unresolvable_scene_falls_back_to_the_declared_cap(
        self, agent_factory,
    ):
        """No chain -> nothing derivable -> the operator's own number runs."""
        agent = agent_factory(width=None, cap=3)

        _run_round(agent, 16)

        assert agent.probe.peak == 3, (
            "with nothing derivable the declared cap of 3 should have been "
            "the width; %d VPs ran at once instead." % agent.probe.peak
        )


# ---------------------------------------------------------------------------
# 4. None is not 0
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNoneIsNotZero:
    def test_a_gate_with_nothing_derivable_admits_everyone(self, agent_factory):
        """Through ``VerificationExecutor.run`` -- a production entry.

        ``None`` from every reader means "no bound is derivable", which is
        a configuration failure the dispatch path already reports loudly.
        It must not become a gate that admits nobody: a misrouted scene
        then hangs the plan silently instead of failing with the reason.
        """
        agent = agent_factory(width=7, cap=4)
        executor = agent._build_verification_executor(_plan(12))
        gate = _VpCapacityGate(lambda: None, lambda: None)

        asyncio.run(executor.run(gate=gate))

        assert executor.completed_vps == [
            "VP-%03d" % i for i in range(1, 13)
        ], (
            "an unbounded gate should have run every VP; got %s"
            % (executor.completed_vps,)
        )

    def test_a_real_zero_width_gate_admits_nobody(self, agent_factory):
        """The other direction, because the two must not be confused.

        ``0`` is the meters saying every provider in the chain is shut -- a
        real reading, and one the framework is not entitled to overrule by
        waving it through as "unbounded". So a zero-width gate parks the
        round rather than running it wide, and that parking is
        distinguishable from the ``None`` case above within seconds.
        """
        agent = agent_factory(width=7, cap=4)
        executor = agent._build_verification_executor(_plan(12))
        gate = _VpCapacityGate(lambda: 0, lambda: 0, recheck_sec=30.0)

        # 0.3s, deliberately under the 0.6s it would take even to run the
        # 12 points one at a time. The assertion has to be about "the gate
        # admits nobody", not about a timeout that a serial run could win.
        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(asyncio.TimeoutError):
                loop.run_until_complete(
                    asyncio.wait_for(executor.run(gate=gate), timeout=0.3)
                )
        finally:
            loop.close()

        assert executor.completed_vps == [], (
            "a zero-width gate let VPs through: %s" % (executor.completed_vps,)
        )


# ---------------------------------------------------------------------------
# 5. The width is observable — a change in the width must be visible
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTheWidthIsObservable:
    def test_the_width_a_gate_binds_at_is_the_one_the_report_quotes(
        self, agent_factory,
    ):
        """``_note_vp_capacity`` on the path the round actually takes.

        Without this the change would be invisible from outside: the gate
        could size the round correctly and every report would still say
        the old declared cap, because ``parallelism_cap`` is what the
        profile has always published. The round's width is now a derived
        number, so it has to be recorded somewhere the report reads --
        which is what ``_build_vp_capacity_gate`` does and what Phase 3
        then hands to ``ExecutionProfileGenerator``.
        """
        agent = agent_factory(width=13, cap=4)

        _run_round(agent, 4)

        assert agent._report_vp_capacity() == 13, (
            "the report would quote %r while the round ran 13 wide"
            % (agent._report_vp_capacity(),)
        )

    def test_the_profile_publishes_the_derived_width_not_the_declared_cap(
        self, agent_factory,
    ):
        """What an operator (or the bridge UI) reads off the profile."""
        from verification_profile import ExecutionProfileGenerator

        agent = agent_factory(width=13, cap=4)
        _run_round(agent, 4)

        profile = ExecutionProfileGenerator(
            {"verification_results": []},
            vp_capacity=agent._report_vp_capacity(),
        ).build()

        assert profile["vp_capacity"] == 13, profile
        assert profile["parallelism_cap"] == 4, (
            "the declared cap must still be published — it is the "
            "fallback, and a reader has to be able to see that the "
            "width that ran was not it: %r" % (profile,)
        )
