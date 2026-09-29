"""Phase 2's fan-out gets a plan-wide ceiling derived from capacity.

``_execute_group`` applies ``TimeoutPolicy.parallelism_cap`` per group, and
``execute_verification_plan_async`` gathers every group at once — so the
plan-wide fan-out is ``parallelism_cap × group_count``. The code's own
comment identified the fix ("we hoist the semaphore into
``_execute_group`` by passing it in") and never did it.

What bounds the plan, and what does not:

* ``parallelism_cap`` still throttles each group. It is an operator knob
  with existing tests pinning it, and this change does not reinterpret
  it.
* A single plan-wide bound is layered on top, sized to
  :func:`provider_capacity.configured_capacity_ceiling` — the sum of the
  per-provider caps the operator declared. With four method-groups at
  cap 4 the product is 16; the bound exists because nothing else limits
  ``cap × groups``, and each in-flight VP is a ``claude`` subprocess
  competing for the same provider slots.
* When no capacity is configured there is no declared fleet size to
  derive a bound from, so there is no plan-wide bound and
  ``parallelism_cap`` alone governs. The bound used to be a hardcoded
  fleet number; that number was a second copy of what the caps already
  determine, and it had to be re-asserted whenever a provider changed.

Pinned here: the plan bound actually binds when the group product would
exceed it, it stays transparent below it, it disappears when nothing is
configured, and it does not disturb the per-group throttle.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_agent import VerificationAgent, _plan_wide_vp_cap  # noqa: E402

#: Two declared providers summing to 20 — the ceiling these tests use.
_CEILING_RULES = [("^vendor a", 12), ("^vendor b", 8)]
_CEILING = 20


@pytest.fixture
def ceiling(capacity_config):
    capacity_config(_CEILING_RULES)
    return _CEILING


class TestPlanWideCap:
    def test_the_ceiling_is_the_sum_of_the_declared_caps(self, ceiling):
        assert _plan_wide_vp_cap() == ceiling

    def test_no_capacity_configured_means_no_plan_bound(self):
        """Nothing declared → no number to derive a bound from.

        Returning a constant here is what the old implementation did, and
        it is why adding or retiring a provider meant editing source.
        """
        assert _plan_wide_vp_cap() is None

    def test_a_larger_declaration_raises_the_ceiling(self, capacity_config):
        capacity_config([("^vendor a", 40)])
        assert _plan_wide_vp_cap() == 40


class _ConcurrencyProbe(VerificationAgent):
    """Agent whose per-VP work is a sleep that records the peak depth.

    The lock around ``_active`` / ``_peak`` is decorative: under single-
    threaded asyncio those two lines run between ``await`` points, so
    no other coroutine can interleave between the ``+=`` and the
    ``max``. We keep the lock anyway because (a) the cost is negligible
    and (b) deleting it would invite a future reader to add
    ``await``s inside the critical section and silently break the
    measurement. The lock itself is created lazily on first coroutine
    entry rather than in ``__init__``: ``asyncio.Lock.__init__`` in
    Python 3.9 calls ``get_event_loop()`` and raises
    ``RuntimeError: There is no current event loop`` when no loop is
    running, which is exactly the state of the synchronous
    ``agent_factory._make`` fixture that builds the probe. By 3.10 the
    constructor no longer reaches for a loop, so the lazy form works
    uniformly on both interpreters.
    """

    def __init__(self, *args, hold_sec=0.05, **kwargs):
        super().__init__(*args, **kwargs)
        self._hold_sec = hold_sec
        self._active = 0
        self._peak = 0
        self._lock = None          # built lazily inside the running loop

    async def _run_single_vp_async(self, vp: dict) -> dict:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._active += 1
            self._peak = max(self._peak, self._active)
        try:
            await asyncio.sleep(self._hold_sec)
        finally:
            async with self._lock:
                self._active -= 1
        return {"id": vp.get("id", "?"), "status": "PASSED", "reasons": []}


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

    def _make(cap):
        agent = _ConcurrencyProbe(
            plan_dir=plan_dir,
            project_dir=project_dir,
            coding_tool=None,
            verif_repo=repo,
        )
        agent.timeout_policy.parallelism_cap = cap
        # ``_execute_group`` brackets its run with persistence events.
        agent.persistence.start_round(1)
        return agent

    return _make


def _vps(prefix, n, method="code_review"):
    return [{"id": f"{prefix}-{i}", "title": "t", "verification_method": method}
            for i in range(n)]


def _plan(*groups):
    """``_plan(("code_review", 5), ("automated_test", 5), …)`` → plan dict."""
    points = []
    for method, n in groups:
        points.extend(_vps(method[:1].upper(), n, method=method))
    return {"verification_points": points}


# ---------------------------------------------------------------------------
# The plan bound binds
# ---------------------------------------------------------------------------


def test_the_group_product_is_bounded_by_the_declared_ceiling(agent_factory, ceiling):
    """6 groups × cap 10 would be 60 in flight; the ceiling says 20."""
    agent = agent_factory(10)
    methods = ("code_review", "automated_test", "api_test",
               "ui_validation", "code_review", "api_test")
    plan = _plan(*[(m, 10) for m in methods])   # 6 distinct-ish groups

    asyncio.run(agent.execute_verification_plan_async(plan))

    assert agent._peak == ceiling, (
        f"peak in-flight was {agent._peak} for 60 VPs across 6 groups at "
        f"cap 10 — the per-group caps multiplied past the declared ceiling"
    )


def test_a_plan_inside_the_ceiling_is_not_throttled(agent_factory, ceiling):
    """4 groups × cap 4 = 16 ≤ 20 — the plan bound must not bind."""
    agent = agent_factory(4)
    plan = _plan(("code_review", 4), ("automated_test", 4),
                 ("api_test", 4), ("ui_validation", 4))

    asyncio.run(agent.execute_verification_plan_async(plan))

    assert agent._peak == 16, (
        f"peak was {agent._peak}; the plan-wide bound should be "
        f"transparent below {ceiling}"
    )


def test_an_unconfigured_installation_has_no_plan_bound(agent_factory):
    """No capacity declared → the group product stands, unthrottled.

    The per-group throttle still applies; only the extra plan-wide layer
    is absent, because there is no declared fleet size to size it from.
    """
    agent = agent_factory(10)
    plan = _plan(*[(m, 10) for m in ("code_review", "automated_test", "api_test")])

    asyncio.run(agent.execute_verification_plan_async(plan))

    assert agent._peak == 30, (
        f"peak was {agent._peak}; with nothing configured the per-group "
        f"caps are the only bound and 3 groups × 10 should all run"
    )


# ---------------------------------------------------------------------------
# parallelism_cap keeps its meaning
# ---------------------------------------------------------------------------


def test_the_per_group_throttle_still_applies(agent_factory):
    """cap 2 → a single 6-VP group still runs 2 at a time.

    This is the contract `test_orchestrator_respects_parallelism_cap_2`
    pins, restated here so a future change to the plan-wide bound cannot
    quietly absorb it.
    """
    agent = agent_factory(2)

    asyncio.run(agent._execute_group("code_review", _vps("A", 6)))

    assert agent._peak == 2


def test_a_lone_group_directly_driven_gets_the_configured_bound(
    agent_factory, ceiling,
):
    """No plan bound passed → the group builds the configured one itself."""
    agent = agent_factory(ceiling)

    asyncio.run(
        agent._execute_group("code_review", _vps("A", ceiling + 5))
    )

    assert agent._peak == ceiling
