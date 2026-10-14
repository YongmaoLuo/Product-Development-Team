"""Regression: one round rebuilds a given binary AT MOST ONCE.

Background
----------
2026-10-14, CC-Switch plan. A verification round dispatched 20 VPs;
all 20 judged the *same* ``libcc_switch_lib.dylib`` stale and each one
started its own ``BinaryRebuildAgent`` against the same ``target/debug``::

    03:38:32-33  20 x binary_stale_detected (libcc_switch_lib.dylib)
                 every agent: "Blocking waiting for file lock on
                 artifact directory"
    03:41-03:59  3 VPs got the rebuilt binary -> vp_complete
    04:02:22     last log line
                 11 VPs never emitted a single event
    05:24:01     watchdog hit VERIFICATION_HARD_STALE_SECONDS, CAS -> failed

The watchdog was right. The round was wrong: **a round had no notion of
"this binary has already been rebuilt"**. ``FreshnessCache`` memoises the
*check* only, and a failed rebuild leaves the binary's mtime untouched —
so the cache key is unchanged, all 20 VPs got the same cached "stale",
and the cache amplified the fan-out instead of collapsing it.

What is pinned here
-------------------
Every test below drives the REAL path — ``_build_verification_executor``
-> ``VerificationExecutor.run(max_parallel=0)`` ->
``BaseExecutor._execute_one_item`` -> ``sub_agent_runner``. Nothing
calls the closure directly, because calling it directly would not
produce the concurrency these assertions are about.

1. **Single-flight.** N VPs in one layer, all judging the binary stale,
   run the rebuild exactly ONCE. Negative control: bypass the
   coordinator (make ``run_once``/``run_once_sync`` plain pass-throughs,
   which is precisely the pre-fix behaviour) and the count goes to N.

2. **Failure does not retry.** A failed rebuild is still one rebuild.
   Every VP ends BLOCKED, none of them retries.

3. **Freshness is still per-VP.** Single-flighting the rebuild must not
   degrade the check: a fresh VP passes straight through, and a stale
   VP whose rebuild succeeded still re-checks and passes.

4. **preflight and the VP arms share one coordinator.** If the
   round-start preflight rebuilt the binary, the VPs dispatched after it
   do not rebuild again — the total for the round is still 1. This is
   the semantics the round is built on ("每一轮验证之前都要有一个
   VP……专门用来做重建的。重建完之后，之后的 VP 是不可以去做重建的"),
   and it is the arm that was never implemented.

5. **Round boundaries.** ``run_full_verification`` clears the coordinator
   at the top of every round, before the preflight — otherwise round 2
   inherits round 1's "already rebuilt" verdict, and the clear has to
   happen before the preflight or the preflight's own rebuild reads as
   last round's residue.

The freshness re-check is deliberately NOT part of the single flight.
Checking is cheap and is each VP's own evidence for deciding BLOCKED;
only the rebuild is shared.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: How long the stubbed rebuild agent "builds". Long enough that every
#: VP in the layer has time to reach the rebuild call before it returns —
#: without a real overlap window the negative control below would be
#: measuring scheduling luck instead of the fix.
REBUILD_SLEEP_SECONDS = 0.6

#: VPs per layer. The incident had 20; the contract is "N -> 1" for any
#: N, and a handful is enough to make the fan-out visible while keeping
#: the suite fast.
VP_COUNT = 5


def _make_agent(tmp_path: Path):
    from verification_agent import Orchestrator

    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(exist_ok=True)
    project_dir.mkdir(exist_ok=True)
    return Orchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=None,
    )


def _layer_plan(n: int = VP_COUNT) -> Dict[str, Any]:
    """``n`` VPs with no ``depends_on`` edge -> all in layer 0, so
    ``BaseExecutor.run`` launches them through ONE ``asyncio.gather``
    on ONE event loop. That is the exact topology of the incident."""
    return {
        "verification_points": [
            {
                "id": f"VP-{i:03d}", "title": f"vp {i}",
                "verification_method": "e2e",
                "test_command": "pytest -q",
                "expected_result": "exit 0",
                "priority": 1, "related_prd_criteria": [],
            }
            for i in range(1, n + 1)
        ],
    }


def _freshness(stale: bool):
    """A real :class:`FreshnessReport`, so the production code under test
    reads the same attributes it reads for real. ``kind`` is always
    ``KIND_RUST_PYTHON`` — the single-flight key is built from it, so
    every stub in a test must agree on it."""
    from binary_freshness import FreshnessReport, KIND_RUST_PYTHON

    if stale:
        return FreshnessReport(
            kind=KIND_RUST_PYTHON, status="FAILED",
            detail="test: .so older than .rs",
            evidence="test: mtime mismatch",
            rebuild_command="cargo build --release",
            binary_path="native_ext/target/release/lib.so",
            newest_source_mtime=2.0, binary_mtime=1.0,
        )
    return FreshnessReport(
        kind=KIND_RUST_PYTHON, status="PASSED",
        detail="test: fresh",
        evidence="test: mtime ok",
        rebuild_command="cargo build --release",
        binary_path="native_ext/target/release/lib.so",
        newest_source_mtime=1.0, binary_mtime=2.0,
    )


def _rebuild_result(success: bool):
    from binary_rebuild_agent import RebuildResult

    return RebuildResult(
        success=success,
        diagnostics="test: stubbed rebuild agent",
        commands_run=["cargo build --release"],
        agent_used=True,
        post_check_passed=success,
    )


async def _passed() -> Dict[str, Any]:
    return {
        "status": "PASSED", "reasons": [],
        "actual_result": "test: downstream stub", "evidence": {},
    }


class _RebuildCounter:
    """Stands in for the real multi-minute rebuild agent.

    Counts entries and holds each one for :data:`REBUILD_SLEEP_SECONDS`
    so the layer's VPs genuinely overlap. Also records how many rebuilds
    were in flight at once, which is what the negative control uses to
    prove it really reproduced the concurrency it claims to.
    """

    def __init__(self, success: bool = False) -> None:
        self.success = success
        self.lock = threading.Lock()
        self.calls: List[str] = []
        self.peak_concurrent = 0
        self._active = 0

    def __call__(self, *a: Any, **k: Any) -> Any:
        with self.lock:
            self.calls.append(str(k.get("vp_id", "?")))
            self._active += 1
            self.peak_concurrent = max(self.peak_concurrent, self._active)
        try:
            time.sleep(REBUILD_SLEEP_SECONDS)
            return _rebuild_result(self.success)
        finally:
            with self.lock:
                self._active -= 1

    @property
    def count(self) -> int:
        return len(self.calls)


def _install(agent, monkeypatch, freshness_fn, rebuild_fn) -> List[tuple]:
    """Stub the two seam functions and capture the audit events."""
    events: List[tuple] = []
    monkeypatch.setattr(
        "verification_agent.check_binary_freshness", freshness_fn,
    )
    monkeypatch.setattr(
        "binary_rebuild_agent.attempt_intelligent_rebuild", rebuild_fn,
    )
    monkeypatch.setattr(
        agent.persistence, "write_verification_point_log",
        lambda vp_id, event_type, data: events.append((vp_id, event_type)),
    )
    return events


def _downstream_pass_through(agent, monkeypatch) -> List[str]:
    """Stub the post-freshness work; record which VPs got there."""
    reached: List[str] = []

    async def fake_run_single_vp(vp: Dict[str, Any]):
        reached.append(str(vp.get("id", "?")))
        return {
            "status": "PASSED", "reasons": [],
            "actual_result": "test: downstream stub",
            "evidence": {},
        }

    monkeypatch.setattr(agent, "_run_single_vp_async", fake_run_single_vp)
    return reached


def _run_round(agent, plan: Dict[str, Any]) -> Dict[str, Any]:
    """The real executor path — no direct calls into the closure."""
    executor = agent._build_verification_executor(plan)
    asyncio.run(executor.run(max_parallel=0))
    return {v["vp_id"]: v for v in executor.collect_verdicts()}


# ---------------------------------------------------------------------------
# 1. Single-flight
# ---------------------------------------------------------------------------

def test_stale_vps_share_one_rebuild(tmp_path: Path, monkeypatch) -> None:
    """N VPs judging one binary stale run the rebuild exactly ONCE.

    This is the incident, reduced: one layer, N VPs, one stale binary,
    N concurrent ``attempt_intelligent_rebuild`` calls in production.
    """
    agent = _make_agent(tmp_path)
    counter = _RebuildCounter(success=False)
    _install(agent, monkeypatch, lambda *a, **k: _freshness(stale=True), counter)
    reached = _downstream_pass_through(agent, monkeypatch)

    verdicts = _run_round(agent, _layer_plan())

    assert counter.count == 1, (
        f"{VP_COUNT} VPs judged the same binary stale but the rebuild ran "
        f"{counter.count}×; the single-flight coordinator did not collapse "
        f"the fan-out. calls={counter.calls}"
    )
    # Every VP reached a terminal verdict — BLOCKED for all of them is the
    # correct outcome here (the stubbed rebuild fails), but the round must
    # not silently drop a VP.
    assert set(verdicts) == {f"VP-{i:03d}" for i in range(1, VP_COUNT + 1)}, (
        f"every VP must reach a terminal verdict; got {sorted(verdicts)}"
    )
    assert reached == [], (
        f"a VP whose binary stayed stale must not reach the downstream "
        f"sub-agent; got {reached}"
    )


def test_bypassing_the_coordinator_restores_the_fanout(
    tmp_path: Path, monkeypatch,
) -> None:
    """NEGATIVE CONTROL for the test above.

    Replaces the coordinator with a plain pass-through — literally the
    pre-fix code path, where every VP called
    ``attempt_intelligent_rebuild`` for itself. The rebuild count must
    climb back to N. If this stayed at 1, the test above would be
    measuring something other than the coordinator.
    """
    agent = _make_agent(tmp_path)
    counter = _RebuildCounter(success=False)

    # The pre-fix behaviour, spelled out. These stand in for unbound
    # methods, so they keep the ``self`` parameter — without it the
    # monkeypatched function is bound and receives the instance.
    async def run_once(self, key, factory):
        return await asyncio.to_thread(factory)

    def run_once_sync(self, key, factory):
        return factory()

    monkeypatch.setattr(
        "verification_agent._BinaryRebuildCoordinator.run_once", run_once,
    )
    monkeypatch.setattr(
        "verification_agent._BinaryRebuildCoordinator.run_once_sync",
        run_once_sync,
    )
    _install(agent, monkeypatch, lambda *a, **k: _freshness(stale=True), counter)
    _downstream_pass_through(agent, monkeypatch)

    _run_round(agent, _layer_plan())

    # ``>= 2`` rather than ``== VP_COUNT``: what the negative control has
    # to show is that the count is no longer pinned at 1. Asserting the
    # exact N would make this control hostage to thread scheduling — if
    # one VP's freshness check happened to land after the rebuild
    # finished, the control would report 4 and "fail" while correctly
    # demonstrating the fan-out.
    assert counter.count >= 2, (
        f"bypassing the coordinator must restore the per-VP fan-out, but "
        f"the rebuild still ran {counter.count}× — the coordinator was not "
        f"what collapsed it. calls={counter.calls}"
    )
    assert counter.peak_concurrent >= 2, (
        f"the bypassed rebuilds did not actually overlap "
        f"(peak={counter.peak_concurrent}); the control did not reproduce "
        f"the concurrency it is meant to demonstrate"
    )


def test_followers_receive_the_leaders_result_object(
    tmp_path: Path, monkeypatch,
) -> None:
    """A follower gets the leader's ``RebuildResult`` — not one of its own.

    A rebuild count of 1 would technically satisfy a counter, but this
    asserts the sharing is real: every VP branches on an object that
    came from a single attempt.
    """
    agent = _make_agent(tmp_path)
    counter = _RebuildCounter(success=False)
    _install(agent, monkeypatch, lambda *a, **k: _freshness(stale=True), counter)
    _downstream_pass_through(agent, monkeypatch)

    seen: List[Any] = []
    original_builder = agent._build_binary_freshness_blocked_verdict

    def recording_builder(vp_id, freshness, rebuild_result=None):
        seen.append(rebuild_result)
        return original_builder(vp_id, freshness, rebuild_result)

    monkeypatch.setattr(
        agent, "_build_binary_freshness_blocked_verdict", recording_builder,
    )

    _run_round(agent, _layer_plan())

    assert counter.count == 1, f"rebuild ran {counter.count}×"
    assert len(seen) == VP_COUNT, (
        f"every VP must have judged the same failed rebuild and been "
        f"BLOCKED by it; only {len(seen)} of {VP_COUNT} reached the verdict"
    )
    identities = {id(r) for r in seen}
    assert len(identities) == 1, (
        f"all {VP_COUNT} VPs must share ONE RebuildResult object; got "
        f"{len(identities)} distinct objects — followers rebuilt for "
        f"themselves instead of awaiting the leader"
    )


# ---------------------------------------------------------------------------
# 2. A failed rebuild is not retried
# ---------------------------------------------------------------------------

def test_failed_rebuild_is_attempted_once_and_all_vps_block(
    tmp_path: Path, monkeypatch,
) -> None:
    """A rebuild that fails must not become a retry storm.

    This is the incident's second half: cargo's file lock turned the
    failed builds into a queue. One attempt, then every VP re-checks,
    finds it still stale, and is BLOCKED — deterministically, without
    waiting on a lock.
    """
    agent = _make_agent(tmp_path)
    counter = _RebuildCounter(success=False)
    _install(agent, monkeypatch, lambda *a, **k: _freshness(stale=True), counter)
    reached = _downstream_pass_through(agent, monkeypatch)

    verdicts = _run_round(agent, _layer_plan())

    assert counter.count == 1, (
        f"a failed rebuild must be attempted exactly once per round; it "
        f"ran {counter.count}× — the failure is being retried. "
        f"calls={counter.calls}"
    )
    blocked = [v for v in verdicts.values() if v["status"] == "BLOCKED"]
    assert len(blocked) == VP_COUNT, (
        f"every VP must be BLOCKED when the one rebuild failed; got "
        f"{ {v: r['status'] for v, r in verdicts.items()} }"
    )
    for verdict in blocked:
        assert "binary_freshness" in verdict["evidence"], (
            f"a BLOCKED verdict must carry its own freshness evidence; "
            f"got {verdict['evidence']!r}"
        )
    assert reached == [], (
        f"no VP may proceed to the downstream sub-agent on a stale binary; "
        f"got {reached}"
    )


def test_rebuild_raising_is_attempted_once_too(
    tmp_path: Path, monkeypatch,
) -> None:
    """A rebuild that *raises* also counts as "attempted".

    The record has to be written on the raising path, otherwise the next
    VP sees no result and starts a second build — the same storm, one
    exception deeper.
    """
    agent = _make_agent(tmp_path)
    counter = _RebuildCounter(success=False)

    def exploding_rebuild(*a: Any, **k: Any):
        counter.calls.append(str(k.get("vp_id", "?")))
        raise RuntimeError("test: rebuild agent exploded")

    _install(agent, monkeypatch, lambda *a, **k: _freshness(stale=True),
             exploding_rebuild)
    reached = _downstream_pass_through(agent, monkeypatch)

    _run_round(agent, _layer_plan())

    assert counter.count == 1, (
        f"a raising rebuild must still be attempted only once; it ran "
        f"{counter.count}×. calls={counter.calls}"
    )


# ---------------------------------------------------------------------------
# 3. The freshness CHECK is not degraded
# ---------------------------------------------------------------------------

def test_fresh_vps_pass_through_a_single_flighted_rebuild(
    tmp_path: Path, monkeypatch,
) -> None:
    """Single-flighting the rebuild must not single-flight the check.

    The stub hands ``stale`` to the first caller and ``fresh`` to everyone
    after. So one VP rebuilds — successfully, so it then RE-CHECKS, sees
    fresh, and passes — while the rest judge it fresh outright. All five
    must reach their downstream sub-agent, and the freshness check must
    have run once per VP.

    The load-bearing assertion is ``checks >= VP_COUNT``: it is what
    says the check was not memoised away by the single flight. If the
    rebuild's sharing had leaked into the check, this would collapse to
    a handful of calls and the fresh VPs would be judging the world
    through one VP's eyes.
    """
    agent = _make_agent(tmp_path)
    counter = _RebuildCounter(success=True)

    lock = threading.Lock()
    checks = {"n": 0}

    def first_caller_stale(*a: Any, **k: Any):
        if lock.acquire(blocking=False):
            checks["n"] += 1
            return _freshness(stale=True)
        checks["n"] += 1
        return _freshness(stale=False)

    events = _install(agent, monkeypatch, first_caller_stale, counter)
    reached = _downstream_pass_through(agent, monkeypatch)

    verdicts = _run_round(agent, _layer_plan())

    assert counter.count == 1, (
        f"exactly one VP should have rebuilt; the rebuild ran "
        f"{counter.count}×. calls={counter.calls}"
    )
    assert checks["n"] >= VP_COUNT, (
        f"every VP must run its OWN freshness check ({VP_COUNT} expected); "
        f"the check ran {checks['n']}× — the single flight leaked into the "
        f"check, which it must not: checking is each VP's own evidence "
        f"for deciding BLOCKED"
    )
    assert len(reached) == VP_COUNT, (
        f"the {VP_COUNT - 1} VPs that judged the binary FRESH must reach "
        f"their downstream sub-agent untouched, and the one that rebuilt "
        f"must pass after its re-check; only {len(reached)} reached it. "
        f"reached={reached} verdicts="
        f"{ {v: r['status'] for v, r in verdicts.items()} }"
    )
    assert all(v["status"] == "PASSED" for v in verdicts.values()), (
        f"every VP must PASS here (the binary ends fresh); got "
        f"{ {v: r['status'] for v, r in verdicts.items()} }"
    )
    # Each VP logged its OWN pass — one audit line per VP, not one per round.
    fresh_logged = [vp for vp, kind in events if kind == "binary_freshness_pass"]
    assert len(fresh_logged) == VP_COUNT, (
        f"each VP must log its own binary_freshness_pass; got {events}"
    )


def test_all_fresh_round_never_rebuilds(tmp_path: Path, monkeypatch) -> None:
    """A round where nothing is stale must not call the rebuild agent.

    Guards against a coordinator that fires unconditionally, and against
    the freshness check being bypassed by the refactor.
    """
    agent = _make_agent(tmp_path)
    counter = _RebuildCounter(success=False)
    _install(agent, monkeypatch, lambda *a, **k: _freshness(stale=False), counter)
    reached = _downstream_pass_through(agent, monkeypatch)

    verdicts = _run_round(agent, _layer_plan())

    assert counter.count == 0, (
        f"nothing was stale, so the rebuild agent must not have run; it "
        f"ran {counter.count}×. calls={counter.calls}"
    )
    assert len(reached) == VP_COUNT, (
        f"every VP must reach its downstream sub-agent on a fresh binary; "
        f"got {reached}"
    )
    assert all(v["status"] == "PASSED" for v in verdicts.values()), (
        f"all VPs must PASS on a fresh binary; got "
        f"{ {v: r['status'] for v, r in verdicts.items()} }"
    )


# ---------------------------------------------------------------------------
# 4. The preflight arm shares the coordinator with the VP arms
# ---------------------------------------------------------------------------

class _Report:
    action = "restarted"
    detail = "test"
    ready_names = ["api"]
    unready_ports: List[int] = []
    runtimes: Dict[str, Any] = {}


class _NeedsRestart:
    needs_restart = {8080}
    probes = []
    action = "needs_restart"
    detail = "test"

    def to_dict(self) -> Dict[str, Any]:
        return {"action": self.action}


def _stub_preflight(agent, monkeypatch, freshness_fn, rebuild_fn) -> List[tuple]:
    events: List[tuple] = []
    monkeypatch.setattr(
        "verification_agent.check_binary_freshness", freshness_fn,
    )
    monkeypatch.setattr(
        "binary_rebuild_agent.attempt_intelligent_rebuild", rebuild_fn,
    )
    monkeypatch.setattr(
        "service_restart_agent.attempt_service_restart",
        lambda *a, **k: _Report(),
    )
    monkeypatch.setattr(
        "service_freshness.assess_service_freshness",
        lambda *a, **k: _NeedsRestart(),
    )
    monkeypatch.setattr(
        agent.persistence, "write_verification_point_log",
        lambda vp_id, event_type, data: events.append((vp_id, event_type)),
    )
    return events


def test_preflight_rebuild_is_visible_to_the_vps_that_follow(
    tmp_path: Path, monkeypatch,
) -> None:
    """The round's rebuild total is 1 even when preflight already rebuilt.

    The semantics the round is built on: preflight is the VP that exists
    purely to rebuild; the VPs after it must not rebuild again. Note the
    preflight's rebuild here reports SUCCESS and the binary is *still*
    judged stale afterwards (the stub keeps saying so) — the VP arms must
    still not run a second build. One per round is one per round,
    regardless of how the first one went.
    """
    agent = _make_agent(tmp_path)
    counter = _RebuildCounter(success=True)
    _stub_preflight(agent, monkeypatch,
                    lambda *a, **k: _freshness(stale=True), counter)
    _downstream_pass_through(agent, monkeypatch)

    # --- the preflight arm ------------------------------------------
    agent._preflight_service_freshness(_layer_plan(1))
    assert counter.count == 1, (
        f"the preflight should have rebuilt once; it ran {counter.count}×"
    )
    assert counter.calls == ["__preflight__"], (
        f"the preflight must be the one that rebuilt; got {counter.calls}"
    )

    # --- the VP arms, same round ------------------------------------
    verdicts = _run_round(agent, _layer_plan())

    assert counter.count == 1, (
        f"the preflight already rebuilt this round, so the {VP_COUNT} VPs "
        f"must not rebuild again; the round total is {counter.count}. "
        f"calls={counter.calls}"
    )
    assert counter.calls == ["__preflight__"], (
        f"no VP may appear among the rebuilders; got {counter.calls}"
    )
    # The binary is still stale, so the VPs are correctly BLOCKED — they
    # got there by RE-CHECKING, not by rebuilding again.
    assert all(v["status"] == "BLOCKED" for v in verdicts.values()), (
        f"a still-stale binary must BLOCK every VP; got "
        f"{ {v: r['status'] for v, r in verdicts.items()} }"
    )


def test_preflight_failed_rebuild_is_not_retried_by_vps(
    tmp_path: Path, monkeypatch,
) -> None:
    """The other direction: a FAILED preflight rebuild is also final.

    ``_maybe_rebuild_before_service_start`` used to promise "the per-VP
    freshness check will retry it". That promise is exactly what caused
    the 20-VP storm, so it is deliberately dropped: the preflight's
    attempt counts as the round's attempt.
    """
    agent = _make_agent(tmp_path)
    counter = _RebuildCounter(success=False)
    _stub_preflight(agent, monkeypatch,
                    lambda *a, **k: _freshness(stale=True), counter)
    reached = _downstream_pass_through(agent, monkeypatch)

    agent._preflight_service_freshness(_layer_plan(1))
    assert counter.count == 1, f"preflight ran {counter.count}×"

    verdicts = _run_round(agent, _layer_plan())

    assert counter.count == 1, (
        f"a failed preflight rebuild must not be retried by the VPs; the "
        f"round total is {counter.count}. calls={counter.calls}"
    )
    assert all(v["status"] == "BLOCKED" for v in verdicts.values()), (
        f"the binary is still stale, so every VP must be BLOCKED; got "
        f"{ {v: r['status'] for v, r in verdicts.items()} }"
    )
    assert reached == [], (
        f"no VP may run its sub-agent on a stale binary; got {reached}"
    )


def test_preflight_alone_rebuilds_when_nothing_else_runs(
    tmp_path: Path, monkeypatch,
) -> None:
    """Guard against over-firing: the preflight still rebuilds by itself.

    ``test_preflight_path_still_emits_rebuild_attempted`` (in the
    offloop suite) pins the rebuild+restart ordering; this pins that
    routing the preflight through the coordinator did not quietly turn it
    into a no-op.
    """
    agent = _make_agent(tmp_path)
    counter = _RebuildCounter(success=True)
    events = _stub_preflight(agent, monkeypatch,
                             lambda *a, **k: _freshness(stale=True), counter)

    agent._preflight_service_freshness(_layer_plan(1))

    assert counter.count == 1, (
        f"the preflight must still do its own rebuild; it ran "
        f"{counter.count}×"
    )
    assert ("__preflight__", "binary_rebuild_attempted") in events, (
        f"the preflight must still emit binary_rebuild_attempted; "
        f"got {events}"
    )


# ---------------------------------------------------------------------------
# 5. Round boundaries
# ---------------------------------------------------------------------------

def test_coordinator_is_cleared_at_the_top_of_each_round(
    tmp_path: Path, monkeypatch,
) -> None:
    """Round 2 must be allowed to rebuild again.

    ``run_full_verification`` clears both per-round objects. If it did
    not, round 2 would inherit round 1's "already rebuilt" record and
    refuse to build a binary that has been modified since — the VP arm
    would then BLOCK forever on a binary it is no longer allowed to fix.
    """
    agent = _make_agent(tmp_path)
    counter = _RebuildCounter(success=False)

    def fake_preflight(plan):
        agent._maybe_rebuild_before_service_start()

    monkeypatch.setattr(agent, "_preflight_service_freshness", fake_preflight)
    monkeypatch.setattr(agent, "generate_verification_plan",
                        lambda: _layer_plan())
    monkeypatch.setattr(agent, "generate_verification_report",
                        lambda results: {"overall_status": "FAILED",
                                         "verification_results": results})
    monkeypatch.setattr(agent, "_run_single_vp_async", lambda vp: _passed())
    _stub_preflight(agent, monkeypatch,
                    lambda *a, **k: _freshness(stale=True), counter)

    for round_number in (1, 2):
        agent.run_full_verification(round_number=round_number)

    assert counter.count == 2, (
        f"two rounds, each with a stale binary, must rebuild once EACH; "
        f"the rebuild ran {counter.count}× — the coordinator was not "
        f"cleared between rounds. calls={counter.calls}"
    )
    assert counter.calls == ["__preflight__", "__preflight__"], (
        f"expected one preflight rebuild per round; got {counter.calls}"
    )


def test_round_clears_before_the_preflight_not_after(
    tmp_path: Path,
) -> None:
    """Ordering: the clear must come BEFORE the round-start preflight.

    Cleared after the preflight, the preflight's own rebuild would look
    like last round's residue, be wiped, and this round's VPs would get
    a second build. Read out of the source because the ordering IS the
    behaviour — the test above cannot distinguish the two placements.
    """
    from verification_agent import Orchestrator

    src = inspect.getsource(Orchestrator.run_full_verification)
    assert "_rebuild_coordinator.clear()" in src, (
        "run_full_verification must clear the rebuild coordinator each round"
    )
    assert "_freshness_cache.clear()" in src, (
        "run_full_verification must clear the freshness cache each round"
    )
    assert src.index("_rebuild_coordinator.clear()") < src.index(
        "_preflight_service_freshness("
    ), (
        "the coordinator must be cleared BEFORE the round-start preflight, "
        "or the preflight's rebuild is wiped as if it were last round's"
    )


# ---------------------------------------------------------------------------
# The async contract must survive the single-flight rewrite
# ---------------------------------------------------------------------------

def test_sub_agent_runner_is_still_a_coroutine_function(
    tmp_path: Path,
) -> None:
    """``_sub_agent_runner`` is a closure, so the assertion is on the
    executor's bound runner: it must still return an awaitable, which is
    the half of the contract ``BaseExecutor._execute_one_item``
    (``result = self.item_runner(item); if inspect.isawaitable(result):
    result = await result``) depends on."""
    agent = _make_agent(tmp_path)
    executor = agent._build_verification_executor(_layer_plan(2))

    coroutine = executor.sub_agent_runner({"id": "VP-001", "method": "e2e"})
    assert inspect.isawaitable(coroutine), (
        "the sub_agent_runner must return an awaitable — the single-flight "
        "rewrite must not have turned it into a sync function"
    )
    coroutine.close()


def test_base_executor_still_honours_the_isawaitable_contract() -> None:
    """The executor adapts a sync runner through the same
    ``inspect.isawaitable`` seam. Pin that a *synchronous* runner is
    still accepted, so the rewrite did not narrow it to async-only."""
    from base_executor import BaseExecutor

    source = inspect.getsource(BaseExecutor._execute_one_item)
    assert "inspect.isawaitable" in source, (
        "BaseExecutor._execute_one_item must keep the isawaitable seam"
    )
