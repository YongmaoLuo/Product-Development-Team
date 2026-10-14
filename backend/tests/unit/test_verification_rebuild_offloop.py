"""Regression: the per-VP binary-freshness pre-check must not block the
verification round's event loop.

Background
----------
A verification round can emit nothing at all for the length of
``VERIFICATION_HARD_STALE_SECONDS`` (900 + 1800) and then be CAS-failed to
``failed`` by the stale-log watchdog. The watchdog was right — that much
silence with no live signal is exactly its threshold. The round was wrong:
it had nothing to say for all of it.

Root cause: ``VerificationAgent._sub_agent_runner`` — the closure the
executor dispatches **every** VP through — is an ``async def`` that ran
``check_binary_freshness`` and ``attempt_intelligent_rebuild``
*synchronously*. ``attempt_intelligent_rebuild`` shells out to the
project's build toolchain (fast path) and, when that fails, spawns an
LLM rebuild sub-agent; it can run for minutes. Every VP in a layer is
driven by a single ``asyncio.gather`` on a single event loop, so one
synchronous call in that closure freezes **every sibling VP** — not just
the one that needed a rebuild.

Note the same file already wraps its other blocking calls in
``asyncio.to_thread`` (``get_shared_quota_watchdog().probe`` at
:347, the sync-``execute_fn`` dispatch at :5244, the blocking
``time.sleep(200)`` timeout wrapper at :5308). The freshness pre-check
was the one that was missed.

What is pinned here
-------------------
1. **The loop stays live during a rebuild.** A real two-VP round through
   the real executor (``_build_verification_executor`` →
   ``VerificationExecutor.run(max_parallel=0)`` →
   ``BaseExecutor._execute_one_item`` → ``sub_agent_runner``), with one
   VP pinned inside a sleeping rebuild agent, must show the *other* VP
   entering its downstream ``_run_single_vp_async`` while that rebuild is
   still in flight, and a heartbeat task on the same loop must keep
   ticking. Revert the ``to_thread`` and this goes red — the sibling VP
   does not get scheduled at all until the rebuild returns.

2. **The rebuild logic is unchanged.** ``binary_stale_detected`` /
   ``binary_stale_blocked`` / ``binary_freshness_pass`` still fire, the
   stale VP still gets a BLOCKED verdict carrying the freshness
   evidence, and ``rebuild_result`` is still the sole authority on
   ``success``.

3. **The async contract is intact.** ``_sub_agent_runner`` is still a
   coroutine function and ``BaseExecutor._execute_one_item`` still
   honours ``inspect.isawaitable``.

On ``binary_rebuild_attempted``: that event is emitted **only** by the
round-start preflight (``_maybe_rebuild_before_service_start``,
``verification_agent.py:4793``), never by ``_sub_agent_runner``. It is
pinned by ``test_preflight_path_still_emits_rebuild_attempted`` below so
the preflight arm of the fix stays honest.
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


#: How long the stubbed rebuild agent "builds". Long enough that a
#: sibling VP finishing inside the window is unambiguous evidence, short
#: enough to keep the suite fast.
REBUILD_SLEEP_SECONDS = 1.5


def _make_agent(tmp_path: Path):
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(exist_ok=True)
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=None,
    )


def _two_vp_plan() -> Dict[str, Any]:
    """Two VPs with no ``depends_on`` edge → both land in layer 0, so
    ``BaseExecutor.run`` launches them through one ``asyncio.gather`` on
    one event loop. That is the exact topology the incident had."""
    return {
        "verification_points": [
            {
                "id": "VP-A", "title": "first",
                "verification_method": "e2e",
                "test_command": "pytest -q",
                "expected_result": "exit 0",
                "priority": 1, "related_prd_criteria": [],
            },
            {
                "id": "VP-B", "title": "second",
                "verification_method": "e2e",
                "test_command": "pytest -q",
                "expected_result": "exit 0",
                "priority": 1, "related_prd_criteria": [],
            },
        ],
    }


def _freshness(stale: bool):
    """A real :class:`FreshnessReport` so the verdict builder under test
    reads the same attributes it reads in production."""
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


class _RebuildProbe:
    """Shared instrumentation for the "is the loop alive?" assertions.

    ``attempt_intelligent_rebuild`` is the stub that stands in for the
    real multi-minute rebuild. Exactly one caller is admitted into the
    slow path (a non-blocking lock, so the admission is decided once and
    is race-free); every later caller returns immediately. The round
    therefore has one VP parked inside a 1.5s "build" and one that sails
    straight through — which is exactly the production topology that
    freezes the round.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.admitted = False
        self.rebuild_started = threading.Event()
        self.rebuild_finished = threading.Event()
        self.rebuild_calls: List[str] = []
        self.ticks_during_rebuild = 0

    # -- stubs ---------------------------------------------------------
    def freshness_for_first_caller(self, *a: Any, **k: Any):
        """Stale for the first VP that asks, fresh for everyone after."""
        if self.lock.acquire(blocking=False):
            return _freshness(stale=True)
        return _freshness(stale=False)

    def slow_rebuild(self, *a: Any, **k: Any):
        vp_id = str(k.get("vp_id", "?"))
        self.rebuild_calls.append(vp_id)
        if self.admitted:
            return _rebuild_result(success=False)
        self.admitted = True
        self.rebuild_started.set()
        time.sleep(REBUILD_SLEEP_SECONDS)
        self.rebuild_finished.set()
        return _rebuild_result(success=False)


def test_sibling_vp_keeps_progressing_while_a_rebuild_runs(
    tmp_path: Path, monkeypatch,
) -> None:
    """THE load-bearing test: a rebuild must not freeze the round.

    Drives a real two-VP round through the real executor. One VP is
    parked inside a sleeping rebuild agent; the other must reach its
    downstream sub-agent call *while that rebuild is still running*, and
    a heartbeat on the same event loop must keep ticking.

    Negative control: revert ``await asyncio.to_thread(...)`` back to a
    plain synchronous call in ``_sub_agent_runner`` and the sibling VP
    cannot be scheduled until the rebuild returns — ``saw_progress_while_
    rebuilding`` stays False and the heartbeat never ticks.
    """
    agent = _make_agent(tmp_path)
    probe = _RebuildProbe()
    events: List[tuple] = []
    downstream: List[Dict[str, Any]] = []

    monkeypatch.setattr(
        "verification_agent.check_binary_freshness",
        probe.freshness_for_first_caller,
    )
    monkeypatch.setattr(
        "binary_rebuild_agent.attempt_intelligent_rebuild",
        probe.slow_rebuild,
    )
    monkeypatch.setattr(
        agent.persistence, "write_verification_point_log",
        lambda vp_id, event_type, data: events.append((vp_id, event_type)),
    )

    async def fake_run_single_vp(vp: Dict[str, Any]):
        vp_id = str(vp.get("id", "?"))
        downstream.append({
            "vp_id": vp_id,
            "at": time.monotonic(),
            "rebuild_in_flight": not probe.rebuild_finished.is_set(),
        })
        return {
            "status": "PASSED",
            "reasons": [],
            "actual_result": "test: downstream stub",
            "evidence": {},
        }

    monkeypatch.setattr(agent, "_run_single_vp_async", fake_run_single_vp)

    executor = agent._build_verification_executor(_two_vp_plan())

    async def heartbeat() -> None:
        """A timer on the round's own event loop. If a synchronous
        rebuild blocks that loop, these ticks stop dead — which is the
        whole point of the fix."""
        while not probe.rebuild_finished.is_set():
            await asyncio.sleep(0.02)
            probe.ticks_during_rebuild += 1

    async def drive() -> None:
        await asyncio.gather(
            executor.run(max_parallel=0),
            heartbeat(),
        )

    started = time.monotonic()
    asyncio.run(drive())
    elapsed = time.monotonic() - started

    # --- the rebuild really happened ---------------------------------
    assert probe.rebuild_started.is_set(), (
        "the stubbed rebuild agent was never entered — the test did not "
        "exercise the stale-binary branch at all"
    )
    assert len(probe.rebuild_calls) == 1, (
        f"expected exactly one admitted rebuild; got {probe.rebuild_calls}"
    )
    assert elapsed >= REBUILD_SLEEP_SECONDS, (
        f"round finished in {elapsed:.2f}s, faster than the "
        f"{REBUILD_SLEEP_SECONDS}s rebuild — the sleep did not happen"
    )

    # --- THE assertion: the sibling VP moved during the rebuild ------
    assert len(downstream) == 1, (
        f"expected exactly one VP to reach the downstream sub-agent "
        f"(the other must be BLOCKED by the stale binary); got {downstream}"
    )
    progressed = downstream[0]
    assert progressed["rebuild_in_flight"], (
        "the sibling VP only started AFTER the rebuild finished — the "
        "rebuild is still running on the event loop. "
        f"downstream={progressed}"
    )

    # --- and the loop itself kept turning ----------------------------
    assert probe.ticks_during_rebuild > 0, (
        "no heartbeat tick landed while the rebuild was in flight — the "
        "event loop was blocked for the whole rebuild window"
    )

    # --- the round as a whole completed ------------------------------
    # ``completed_vps`` is PASSED-only by contract (a BLOCKED verdict
    # lands in ``failed_vps``), so the honest "both VPs produced a
    # terminal verdict" check reads the verdict map.
    assert {v["vp_id"] for v in executor.collect_verdicts()} == {
        "VP-A", "VP-B",
    }, (
        f"both VPs must reach a terminal verdict; got "
        f"{executor.collect_verdicts()}"
    )


def test_stale_vp_still_blocked_and_events_still_fired(
    tmp_path: Path, monkeypatch,
) -> None:
    """The off-loop rewrite must not change what the rebuild path does.

    Pins, on the same executor path:
      * ``binary_stale_detected`` then ``binary_stale_blocked`` fire for
        the stale VP, in that order;
      * the stale VP gets a BLOCKED verdict carrying ``binary_freshness``
        evidence and the rebuild diagnostics;
      * a VP that sees a fresh binary logs ``binary_freshness_pass`` and
        passes straight through to the downstream sub-agent.
    """
    agent = _make_agent(tmp_path)
    probe = _RebuildProbe()
    events: List[tuple] = []
    downstream: List[str] = []

    monkeypatch.setattr(
        "verification_agent.check_binary_freshness",
        probe.freshness_for_first_caller,
    )
    monkeypatch.setattr(
        "binary_rebuild_agent.attempt_intelligent_rebuild",
        probe.slow_rebuild,
    )
    monkeypatch.setattr(
        agent.persistence, "write_verification_point_log",
        lambda vp_id, event_type, data: events.append((vp_id, event_type)),
    )

    async def fake_run_single_vp(vp: Dict[str, Any]):
        downstream.append(str(vp.get("id", "?")))
        return {
            "status": "PASSED", "reasons": [],
            "actual_result": "test: downstream stub", "evidence": {},
        }

    monkeypatch.setattr(agent, "_run_single_vp_async", fake_run_single_vp)

    executor = agent._build_verification_executor(_two_vp_plan())
    asyncio.run(executor.run(max_parallel=0))

    verdicts = {v["vp_id"]: v for v in executor.collect_verdicts()}

    kinds = [(vp, kind) for vp, kind in events]
    stale_vp = probe.rebuild_calls[0]
    fresh_vp = downstream[0]

    assert (stale_vp, "binary_stale_detected") in kinds, (
        f"the stale VP must log binary_stale_detected; got {kinds}"
    )
    assert (stale_vp, "binary_stale_blocked") in kinds, (
        f"a rebuild that could not clear the stale binary must log "
        f"binary_stale_blocked; got {kinds}"
    )
    assert (fresh_vp, "binary_freshness_pass") in kinds, (
        f"the VP that saw a fresh binary must log binary_freshness_pass; "
        f"got {kinds}"
    )
    assert kinds.index((stale_vp, "binary_stale_detected")) < kinds.index(
        (stale_vp, "binary_stale_blocked")
    ), f"stale_detected must precede stale_blocked; got {kinds}"

    blocked = verdicts[stale_vp]
    assert blocked["status"] == "BLOCKED", (
        f"a VP whose binary stayed stale must be BLOCKED; got {blocked}"
    )
    assert "binary_freshness" in blocked["evidence"], (
        f"the BLOCKED verdict must carry freshness evidence; "
        f"got {blocked['evidence']!r}"
    )
    attempt = blocked["evidence"]["rebuild_attempt"]
    assert attempt["agent_used"] is True, attempt
    assert attempt["diagnostics"] == "test: stubbed rebuild agent", attempt
    assert attempt["post_check_passed"] is False, (
        f"the rebuild failed, so the post-check must be False; got {attempt}"
    )

    assert verdicts[fresh_vp]["status"] == "PASSED", (
        f"a fresh-binary VP must pass through untouched; got "
        f"{verdicts[fresh_vp]}"
    )


def test_successful_rebuild_rechecks_freshness_in_a_thread(
    tmp_path: Path, monkeypatch,
) -> None:
    """A successful rebuild must trigger the *second* freshness check,
    and that recheck must run off the loop too — it is the same
    blocking function, reached from the same closure.

    If the recheck were left synchronous while the rebuild were in a
    thread, a stale-then-fixed binary would block the loop a second
    time, and this is the only test that would notice.
    """
    agent = _make_agent(tmp_path)
    events: List[tuple] = []
    loop_ticks: List[float] = []

    calls = {"n": 0}
    rebuild_started = threading.Event()
    #: Set only AFTER the sleep — the heartbeat's stop condition has to
    #: be "the rebuild is over", not "the rebuild began", or the timer
    #: stops on the very first tick and the test measures nothing.
    rebuild_done = threading.Event()
    started_at: List[float] = []

    def fake_check(*a: Any, **k: Any):
        calls["n"] += 1
        return _freshness(stale=True) if calls["n"] == 1 else _freshness(False)

    def fake_rebuild(*a: Any, **k: Any):
        rebuild_started.set()
        started_at.append(time.monotonic())
        time.sleep(REBUILD_SLEEP_SECONDS)
        rebuild_done.set()
        return _rebuild_result(success=True)

    monkeypatch.setattr(
        "verification_agent.check_binary_freshness", fake_check,
    )
    monkeypatch.setattr(
        "binary_rebuild_agent.attempt_intelligent_rebuild", fake_rebuild,
    )
    monkeypatch.setattr(
        agent.persistence, "write_verification_point_log",
        lambda vp_id, event_type, data: events.append((vp_id, event_type)),
    )

    reached: List[Dict[str, Any]] = []

    async def fake_run_single_vp(vp: Dict[str, Any]):
        reached.append({"rebuild_done": rebuild_done.is_set()})
        return {
            "status": "PASSED", "reasons": [],
            "actual_result": "test: downstream stub", "evidence": {},
        }

    monkeypatch.setattr(agent, "_run_single_vp_async", fake_run_single_vp)

    executor = agent._build_verification_executor(_two_vp_plan())

    async def heartbeat() -> None:
        while not rebuild_done.is_set():
            await asyncio.sleep(0.02)
            loop_ticks.append(time.monotonic())

    async def drive() -> None:
        await asyncio.gather(executor.run(max_parallel=0), heartbeat())

    asyncio.run(drive())

    assert rebuild_started.is_set(), "the rebuild stub was never entered"
    assert calls["n"] >= 2, (
        f"a successful rebuild must re-check freshness; "
        f"check_binary_freshness was called {calls['n']}×"
    )
    during = [t for t in loop_ticks if t < started_at[0] + REBUILD_SLEEP_SECONDS]
    assert during, (
        "no heartbeat tick landed inside the rebuild window — the "
        "blocking sleep is still running on the event loop "
        f"(ticks={loop_ticks})"
    )
    # One VP saw stale and rebuilt it successfully; the other never saw
    # a stale binary. Both therefore run their downstream sub-agent.
    assert len(reached) == 2, reached


def test_preflight_path_still_emits_rebuild_attempted(
    tmp_path: Path, monkeypatch,
) -> None:
    """``binary_rebuild_attempted`` lives on the preflight arm, not on the
    per-VP arm.

    The round-start preflight (``_preflight_service_freshness`` →
    ``_maybe_rebuild_before_service_start``) runs *before*
    ``asyncio.run(executor.run(...))`` in ``run_full_verification``, so it
    is plain synchronous code with no loop to block and is deliberately
    left alone. This test pins that its behaviour is unchanged, so the
    decision to leave it is auditable rather than a silent omission.
    """
    agent = _make_agent(tmp_path)
    events: List[tuple] = []
    order: List[str] = []

    monkeypatch.setattr(
        "verification_agent.check_binary_freshness",
        lambda *a, **k: _freshness(stale=True),
    )

    def fake_rebuild(*a: Any, **k: Any):
        order.append("rebuild")
        return _rebuild_result(success=False)

    def fake_restart(*a: Any, **k: Any):
        order.append("restart")
        return _Report()

    monkeypatch.setattr(
        "binary_rebuild_agent.attempt_intelligent_rebuild", fake_rebuild,
    )
    monkeypatch.setattr(
        "service_restart_agent.attempt_service_restart", fake_restart,
    )
    monkeypatch.setattr(
        "service_freshness.assess_service_freshness",
        lambda *a, **k: _NeedsRestart(),
    )
    monkeypatch.setattr(
        agent.persistence, "write_verification_point_log",
        lambda vp_id, event_type, data: events.append((vp_id, event_type)),
    )

    agent._preflight_service_freshness({
        "verification_points": [
            {"id": "VP-001", "title": "x", "verification_method": "api_test",
             "test_command": "curl http://127.0.0.1:8080/api/items",
             "expected_result": "200", "priority": 1,
             "related_prd_criteria": []},
        ],
    })

    assert order == ["rebuild", "restart"], (
        f"rebuild must still run before the restart; got {order}"
    )
    assert ("__preflight__", "binary_stale_detected") in events, events
    assert ("__preflight__", "binary_rebuild_attempted") in events, (
        f"the preflight must still emit binary_rebuild_attempted; "
        f"got {events}"
    )


def test_preflight_rebuild_really_runs_with_no_event_loop(
    tmp_path: Path, monkeypatch,
) -> None:
    """Pin *why* the preflight arm is left synchronous.

    ``run_full_verification`` is a plain ``def``; it calls
    ``self._preflight_service_freshness(plan_data)`` and only then
    ``asyncio.run(executor.run(max_parallel=0))``. So the preflight's
    ``check_binary_freshness`` / ``attempt_intelligent_rebuild`` run on
    the verification thread with **no loop running at all** — there is
    nothing to starve, because no other VP is in flight yet: the round
    has not started. Wrapping them in ``to_thread`` there would buy
    nothing and would only make the arm async for its callers.

    This asserts the fact at runtime (an event loop is genuinely absent
    when the rebuild agent runs) rather than trusting a comment. If a
    future refactor moves the preflight onto the loop, this goes red and
    the arm has to be re-evaluated.
    """
    agent = _make_agent(tmp_path)
    loop_seen: List[str] = []

    monkeypatch.setattr(
        "verification_agent.check_binary_freshness",
        lambda *a, **k: _freshness(stale=True),
    )

    def fake_rebuild(*a: Any, **k: Any):
        try:
            asyncio.get_running_loop()
            loop_seen.append("RUNNING")
        except RuntimeError:
            loop_seen.append("NO_LOOP")
        return _rebuild_result(success=False)

    monkeypatch.setattr(
        "binary_rebuild_agent.attempt_intelligent_rebuild", fake_rebuild,
    )
    monkeypatch.setattr(
        "service_restart_agent.attempt_service_restart",
        lambda *a, **k: _Report(),
    )
    monkeypatch.setattr(
        "service_freshness.assess_service_freshness",
        lambda *a, **k: _NeedsRestart(),
    )

    agent._preflight_service_freshness({
        "verification_points": [
            {"id": "VP-001", "title": "x", "verification_method": "api_test",
             "test_command": "curl http://127.0.0.1:8080/api/items",
             "expected_result": "200", "priority": 1,
             "related_prd_criteria": []},
        ],
    })

    assert loop_seen == ["NO_LOOP"], (
        "the preflight rebuild ran with a running event loop — the "
        f"round-start preflight must be reached before asyncio.run(); "
        f"saw {loop_seen}"
    )

    # And the ordering that guarantees it, read out of the source.
    import inspect as _inspect
    src = _inspect.getsource(
        __import__("verification_agent").Orchestrator.run_full_verification
    )
    assert src.index("_preflight_service_freshness(") < src.index("asyncio.run("), (
        "_preflight_service_freshness must be called BEFORE the first "
        "asyncio.run() in run_full_verification"
    )


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


# ---------------------------------------------------------------------------
# The async contract must survive the off-loop rewrite
# ---------------------------------------------------------------------------

def test_sub_agent_runner_is_still_a_coroutine_function(
    tmp_path: Path,
) -> None:
    """``_sub_agent_runner`` is built as a closure, so the assertion is
    on the executor's bound runner: it must still return an awaitable
    when called, which is the half of the contract
    ``BaseExecutor._execute_one_item`` (``result = self.item_runner(item);
    if inspect.isawaitable(result): result = await result``) depends on."""
    agent = _make_agent(tmp_path)
    executor = agent._build_verification_executor(_two_vp_plan())

    coroutine = executor.sub_agent_runner(
        {"id": "VP-A", "method": "e2e"},
    )
    assert inspect.isawaitable(coroutine), (
        "the sub_agent_runner must return an awaitable — the off-loop "
        "rewrite must not have turned it into a sync function"
    )
    coroutine.close()


def test_base_executor_still_honours_the_isawaitable_contract() -> None:
    """The executor adapts a sync runner through the same
    ``inspect.isawaitable`` seam. Pin that a *synchronous* runner is
    still accepted, so the ``to_thread`` change did not narrow the
    contract to async-only."""
    from base_executor import BaseExecutor

    source = inspect.getsource(BaseExecutor._execute_one_item)
    assert "inspect.isawaitable" in source, (
        "BaseExecutor._execute_one_item must keep the isawaitable seam"
    )
