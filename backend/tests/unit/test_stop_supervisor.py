"""The background supervisor that owns a stop from here to the verdict.

2026-11-20 — a stop that has to *wait* for the plan to fall silent
cannot do that wait on the request thread: the quiet window is
measured in seconds and the request has to come back in milliseconds.
So the wait moves into a background thread (:mod:`stop_supervisor`),
and the thread owns the two decisions that matter:

* **when the phase may be flipped** — only after the plan has really
  gone quiet, and
* **what to do when it does not** — sweep harder, keep watching, and
  report the survivors at the hard limit rather than pretend the plan
  stopped.

This file pins those decisions.  Every test drives the real supervisor
thread; none of them waits for a real process to die, because the
scanner is injected and the sweep is injected too — a test never
signals a real process.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from typing import Callable, List, Optional

import pytest

import stop_supervisor
import verification_cancel
from verification_quiesce import Survivor


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_supervisor():
    """The supervisor registry and the dispatch ledger are process-global.

    The hook also stops and joins every supervisor thread, so a test
    that leaves one running does not leak it into the next test.
    """
    stop_supervisor._reset_for_tests()
    verification_cancel._reset_for_tests()
    yield
    stop_supervisor._reset_for_tests()
    verification_cancel._reset_for_tests()


class _FakeScan:
    """A scanner whose answer the test controls.

    The scanner is the seam that decides what "the process tree"
    means; here it is a list the test rewrites at will.
    """

    def __init__(self, survivors: Optional[List[Survivor]] = None):
        self.survivors = list(survivors or [])
        self.calls = 0

    def __call__(self, plan_id: str) -> List[Survivor]:
        self.calls += 1
        return list(self.survivors)


def _busy_scan() -> _FakeScan:
    """A scanner that always reports one live process."""
    return _FakeScan([Survivor(pid=4242, kind="registry", detail="VP-007")])


def _quiet_sweep() -> Callable[[str, List[Survivor]], None]:
    """An escalated sweep that touches nothing.

    The sweep is the one place where the supervisor reaches outside
    itself; a test that reaches it must not have the real one run.
    """
    def _sweep(plan_id: str, survivors: List[Survivor]) -> None:
        return None

    return _sweep


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    """Wait for ``predicate`` to come true, without spinning the CPU."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# 1) the quiet verdict
# ---------------------------------------------------------------------------


def test_finalize_only_after_quiet():
    """The phase may only be flipped once the plan has fallen silent.

    While the scan still reports a live process the supervisor keeps
    watching and finalizes nothing; when the tree empties, the
    callback runs once, with nothing left to report.
    """
    scan = _busy_scan()
    finalized = threading.Event()
    calls: List[List[Survivor]] = []

    def _finalize(survivors: List[Survivor]) -> None:
        calls.append(list(survivors))
        finalized.set()

    thread = stop_supervisor.start(
        "p1",
        finalize=_finalize,
        scan=scan,
        quiet_seconds=0.05,
        max_seconds=5.0,
        hard_max_seconds=30.0,
    )
    assert thread is not None

    # the tree still has a survivor — the phase may not move yet
    assert not finalized.wait(0.3)
    assert calls == []

    # the plan falls silent
    scan.survivors = []
    assert finalized.wait(15.0), "a quiet plan was never finalized"

    assert len(calls) == 1
    assert calls[0] == []


def test_no_finalize_on_timeout():
    """A plan that will not fall silent is swept, not finalized.

    The timeout means the tree is still busy: the supervisor escalates
    the cleanup and keeps watching.  Finalizing here is the defect the
    supervisor exists to prevent — a phase that says "stopped" while
    the process tree is still burning.
    """
    scan = _busy_scan()
    sweeps: List[List[Survivor]] = []
    calls: List[List[Survivor]] = []

    def _sweep(plan_id: str, survivors: List[Survivor]) -> None:
        sweeps.append(list(survivors))

    stop_supervisor._sweep = _sweep

    thread = stop_supervisor.start(
        "p1",
        finalize=lambda survivors: calls.append(list(survivors)),
        scan=scan,
        quiet_seconds=0.05,
        max_seconds=0.1,
        hard_max_seconds=30.0,
    )
    assert thread is not None

    assert _wait_for(lambda: bool(sweeps)), "the escalated sweep never ran"
    assert calls == [], "a plan that is not quiet must not be finalized"
    assert thread.is_alive(), "the supervisor must keep watching"


def test_hard_limit_finalizes_with_survivors():
    """At the hard limit the supervisor reports what is still alive.

    The hard limit is the end of the patience, not the end of the
    report: whatever is still running is handed to the callback, which
    is what lets the plan stay in its stopping phase instead of
    claiming to be stopped.
    """
    scan = _busy_scan()
    calls: List[List[Survivor]] = []
    stop_supervisor._sweep = _quiet_sweep()

    thread = stop_supervisor.start(
        "p1",
        finalize=lambda survivors: calls.append(list(survivors)),
        scan=scan,
        quiet_seconds=0.02,
        max_seconds=0.05,
        hard_max_seconds=0.3,
    )
    assert thread is not None
    thread.join(timeout=15.0)

    assert not thread.is_alive()
    assert len(calls) == 1
    assert calls[0] == scan.survivors


# ---------------------------------------------------------------------------
# 2) one supervisor per plan
# ---------------------------------------------------------------------------


def test_single_supervisor_per_plan():
    """A second stop for the same plan joins the first supervision.

    Two supervisors for one plan would be two judges of the same
    silence, and the second one would carry a second deadline over the
    same processes.
    """
    scan = _busy_scan()
    stop_supervisor._sweep = _quiet_sweep()

    first = stop_supervisor.start(
        "p1",
        finalize=lambda survivors: None,
        scan=scan,
        quiet_seconds=0.05,
        max_seconds=0.1,
        hard_max_seconds=30.0,
    )
    second = stop_supervisor.start(
        "p1",
        finalize=lambda survivors: None,
        scan=scan,
        quiet_seconds=0.05,
        max_seconds=0.1,
        hard_max_seconds=30.0,
    )

    assert first is not None
    assert second is first
    assert stop_supervisor.active_plan_ids() == ["p1"]


def test_other_plan_gets_its_own_supervisor():
    """The registry is per plan: stopping one plan does not disturb
    another plan's supervision."""
    stop_supervisor._sweep = _quiet_sweep()

    first = stop_supervisor.start(
        "p1",
        finalize=lambda survivors: None,
        scan=_busy_scan(),
        quiet_seconds=0.05,
        max_seconds=0.1,
        hard_max_seconds=30.0,
    )
    second = stop_supervisor.start(
        "p2",
        finalize=lambda survivors: None,
        scan=_busy_scan(),
        quiet_seconds=0.05,
        max_seconds=0.1,
        hard_max_seconds=30.0,
    )

    assert first is not None
    assert second is not None
    assert first is not second
    assert stop_supervisor.active_plan_ids() == ["p1", "p2"]


def test_empty_plan_id_is_not_supervised():
    """An empty plan id names no plan, so nothing is registered."""
    assert stop_supervisor.start("", finalize=lambda survivors: None) is None
    assert stop_supervisor.active_plan_ids() == []


# ---------------------------------------------------------------------------
# 3) the thread's own life
# ---------------------------------------------------------------------------


def test_thread_exits_after_finalize():
    """The supervisor leaves no thread behind.

    A thread that stays alive after the plan is stopped is a thread
    that keeps watching a plan nobody is waiting for any more.
    """
    scan = _FakeScan()
    finalized = threading.Event()

    thread = stop_supervisor.start(
        "p1",
        finalize=lambda survivors: finalized.set(),
        scan=scan,
        quiet_seconds=0.02,
        max_seconds=5.0,
        hard_max_seconds=30.0,
    )
    assert thread is not None
    assert thread.daemon is True

    assert finalized.wait(15.0), "a quiet plan was never finalized"
    thread.join(timeout=15.0)

    assert not thread.is_alive()
    assert stop_supervisor.active_plan_ids() == []


def test_finalize_failure_still_ends_the_thread(caplog):
    """A callback that raises must not leave the supervisor running.

    The phase flip is somebody else's code; a failure there is
    reported, and the supervisor still ends — a thread that never
    exits is worse than a phase that did not move.
    """
    scan = _FakeScan()

    def _boom(survivors: List[Survivor]) -> None:
        raise RuntimeError("the phase flip failed")

    with caplog.at_level(logging.ERROR, logger="stop_supervisor"):
        thread = stop_supervisor.start(
            "p1",
            finalize=_boom,
            scan=scan,
            quiet_seconds=0.02,
            max_seconds=5.0,
            hard_max_seconds=30.0,
        )
        assert thread is not None
        thread.join(timeout=15.0)

    assert not thread.is_alive()
    assert stop_supervisor.active_plan_ids() == []
    assert "finalize failed" in caplog.text


# ---------------------------------------------------------------------------
# 4) the published shapes
# ---------------------------------------------------------------------------


def test_public_signature_matches_the_contract():
    """The call shape the architecture decided on, defaults included."""
    params = inspect.signature(stop_supervisor.start).parameters

    assert list(params) == [
        "plan_id",
        "finalize",
        "scan",
        "quiet_seconds",
        "max_seconds",
        "hard_max_seconds",
    ]
    assert params["plan_id"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params["finalize"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["finalize"].default is inspect.Parameter.empty
    assert params["scan"].default is None
    assert params["quiet_seconds"].default == 5.0
    assert params["max_seconds"].default == 120.0
    assert params["hard_max_seconds"].default == 180.0


def test_finalize_receives_the_survivors():
    """The one-argument callback is handed the survivors of the stop."""
    survivor = Survivor(pid=4242, kind="registry", detail="VP-007")
    scan = _FakeScan([survivor])
    seen: List[list] = []
    stop_supervisor._sweep = _quiet_sweep()

    thread = stop_supervisor.start(
        "p1",
        finalize=seen.append,
        scan=scan,
        quiet_seconds=0.02,
        max_seconds=0.05,
        hard_max_seconds=0.3,
    )
    assert thread is not None
    thread.join(timeout=15.0)

    assert seen == [[survivor]]


def test_finalize_can_take_the_plan_id_and_the_survivors():
    """A callback that serves more than one plan gets both."""
    survivor = Survivor(pid=4242, kind="registry", detail="VP-007")
    scan = _FakeScan([survivor])
    seen: List[tuple] = []
    stop_supervisor._sweep = _quiet_sweep()

    def _finalize(plan_id: str, survivors: List[Survivor]) -> None:
        seen.append((plan_id, list(survivors)))

    thread = stop_supervisor.start(
        "p1",
        finalize=_finalize,
        scan=scan,
        quiet_seconds=0.02,
        max_seconds=0.05,
        hard_max_seconds=0.3,
    )
    assert thread is not None
    thread.join(timeout=15.0)

    assert seen == [("p1", [survivor])]


def test_finalize_can_take_the_plan_id_alone():
    """A callback that asks only for the plan id gets it — and the
    survivors of that stop ride along on the value it receives."""
    survivor = Survivor(pid=4242, kind="registry", detail="VP-007")
    scan = _FakeScan([survivor])
    seen: List[object] = []
    stop_supervisor._sweep = _quiet_sweep()

    def _finalize(plan_id) -> None:
        seen.append(plan_id)
        seen.append(plan_id.survivors)

    thread = stop_supervisor.start(
        "p1",
        finalize=_finalize,
        scan=scan,
        quiet_seconds=0.02,
        max_seconds=0.05,
        hard_max_seconds=0.3,
    )
    assert thread is not None
    thread.join(timeout=15.0)

    assert seen[0] == "p1"
    assert seen[1] == [survivor]
