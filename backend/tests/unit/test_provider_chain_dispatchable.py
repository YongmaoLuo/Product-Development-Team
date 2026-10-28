"""Is there anybody left on the chain to dispatch to?

Why this file exists
--------------------
A provider stops being able to serve for three separate reasons, and the
dispatch loop used to notice them one at a time, in three places:

* it is **parked** — a refusal earlier in this run put it on hold;
* it has **no budget** — the live reading says its window is empty;
* its **cap is zero** — the quota-scaled cap it would serve under comes
  out at nothing.

"Is the whole chain finished?" was therefore unanswerable from anywhere
but the dispatch loop itself, and the three reasons were easy to answer
differently in two places.  These tests pin the two predicates that
answer the question once, for everybody:

* :func:`dynamic_provider_concurrency.provider_has_capacity` — one
  provider, one reading, one park verdict;
* :func:`dynamic_provider_concurrency.chain_has_dispatchable_provider` —
  the whole chain, with the same three questions asked of every member.

What is pinned here
-------------------
* one healthy member makes the whole chain dispatchable, however dead
  its siblings are;
* a chain whose every member is parked is not dispatchable — a healthy
  *reading* does not un-park a provider;
* a member whose reading says 0% left is not dispatchable, and neither
  is a chain made only of such members;
* an empty chain is not dispatchable: there is nobody to dispatch to;
* a member with **no reading** is dispatchable — CC Switch cannot meter
  every provider, and a missing reading is not evidence of an empty
  window;
* with no quota map at all, the park list is the only thing that
  refuses a dispatch.

No test here touches the network or the quota memory file: the readings
are built directly and the park list is a plain
:class:`ProviderCooldown`, so the state this file is about is never
written anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dynamic_provider_concurrency import (  # noqa: E402
    ProviderCooldown,
    chain_has_dispatchable_provider,
    provider_has_capacity,
    set_shared_cooldown,
)
from provider_order import ProviderQuota  # noqa: E402

#: The two members of the chain the tests below walk.
FIRST = "Chain Member A"
SECOND = "Chain Member B"

#: How long a parked provider stays parked — long enough that no test
#: can race the clock, short enough that nothing leaks between tests.
PARK_SECONDS = 300.0


def _quota(provider: str, **kwargs) -> ProviderQuota:
    """A reading for ``provider`` with the given fields overridden."""
    fields = {"provider": provider, "remaining_pct": 100.0}
    fields.update(kwargs)
    return ProviderQuota(**fields)


def _parked_cooldown(*providers: str) -> ProviderCooldown:
    """A cooldown with every named provider parked."""
    cooldown = ProviderCooldown()
    for name in providers:
        cooldown.park(name, seconds=PARK_SECONDS, reason="capacity error")
    return cooldown


# ---------------------------------------------------------------------------
# The four cases the chain predicate has to get right
# ---------------------------------------------------------------------------


def test_one_healthy_member_makes_chain_dispatchable():
    """A single member with budget left keeps the chain alive.

    The dead member is *first* on purpose: the predicate must ask about
    every member, not stop at the first refusal.
    """
    chain = [FIRST, SECOND]
    quota_map = {
        FIRST: _quota(FIRST, remaining_pct=0.0),
        SECOND: _quota(SECOND, remaining_pct=100.0),
    }

    assert chain_has_dispatchable_provider(chain, quota_map) is True


def test_all_members_parked_is_not_dispatchable():
    """Every member on hold means nobody can be dispatched to.

    The readings say 100% left on both members: a park is a *local*
    fact about this run, and a healthy reading does not undo it.
    """
    chain = [FIRST, SECOND]
    quota_map = {
        FIRST: _quota(FIRST, remaining_pct=100.0),
        SECOND: _quota(SECOND, remaining_pct=100.0),
    }
    cooldown = _parked_cooldown(FIRST, SECOND)

    assert chain_has_dispatchable_provider(chain, quota_map, cooldown) is False


def test_zero_pct_member_is_not_dispatchable():
    """A member whose window is empty has no slots to serve under.

    The cap scales with the headroom, so 0% left scales the cap to
    nothing — the same answer the dispatch loop gets when it asks the
    same question.
    """
    quota_map = {FIRST: _quota(FIRST, remaining_pct=0.0)}

    assert chain_has_dispatchable_provider([FIRST], quota_map) is False


def test_empty_chain_is_not_dispatchable():
    """An empty chain has nobody to dispatch to."""
    assert chain_has_dispatchable_provider([], {}) is False


# ---------------------------------------------------------------------------
# The edges around those four
# ---------------------------------------------------------------------------


def test_parked_member_is_not_dispatchable():
    """One park refuses one member even on a healthy reading."""
    quota_map = {FIRST: _quota(FIRST, remaining_pct=100.0)}
    cooldown = _parked_cooldown(FIRST)

    assert chain_has_dispatchable_provider([FIRST], quota_map, cooldown) is False


def test_exhausted_reading_is_not_dispatchable():
    """A reading that says "no budget" refuses the member."""
    quota_map = {FIRST: _quota(FIRST, exhausted=True)}

    assert chain_has_dispatchable_provider([FIRST], quota_map) is False


def test_member_without_reading_is_dispatchable():
    """A provider CC Switch cannot meter is not refused.

    A missing reading is not evidence of an empty window, so the member
    stays in — the fail-open half of the contract.
    """
    assert chain_has_dispatchable_provider([FIRST], {}) is True


def test_no_quota_map_judges_the_park_list_only():
    """With no quota map at all, the park list is the only refusal."""
    cooldown = _parked_cooldown(FIRST)

    assert chain_has_dispatchable_provider([FIRST], None, cooldown) is False
    assert chain_has_dispatchable_provider([SECOND], None, cooldown) is True


def test_shared_cooldown_is_used_when_none_is_injected():
    """With no cooldown injected, the process-wide park list answers."""
    set_shared_cooldown(_parked_cooldown(FIRST))
    try:
        assert chain_has_dispatchable_provider([FIRST], {}) is False
        assert chain_has_dispatchable_provider([SECOND], {}) is True
    finally:
        set_shared_cooldown(None)


def test_parent_is_not_a_chain_member():
    """``parent`` is the caller's own fallback, not a pool member.

    It has no CC Switch row, no quota reading and no cap of its own, so
    counting it as dispatchable would make "the chain is finished"
    unreachable for every chain that ends the way the configured ones
    do.
    """
    assert chain_has_dispatchable_provider(["parent"], {}) is False


# ---------------------------------------------------------------------------
# The single-provider predicate the chain one is built from
# ---------------------------------------------------------------------------


def test_provider_has_capacity_admits_a_healthy_reading():
    assert provider_has_capacity(FIRST, _quota(FIRST, remaining_pct=100.0)) is True


def test_provider_has_capacity_admits_a_provider_without_a_reading():
    assert provider_has_capacity(FIRST, None) is True


def test_provider_has_capacity_admits_a_reading_with_no_number():
    """A reading that carries no headroom is not a reading of zero."""
    assert provider_has_capacity(FIRST, _quota(FIRST, remaining_pct=None)) is True


def test_provider_has_capacity_rejects_a_parked_provider():
    quota = _quota(FIRST, remaining_pct=100.0)

    assert provider_has_capacity(FIRST, quota, parked=True) is False


def test_provider_has_capacity_rejects_an_exhausted_reading():
    assert provider_has_capacity(FIRST, _quota(FIRST, exhausted=True)) is False


def test_provider_has_capacity_rejects_a_zero_headroom_reading():
    assert provider_has_capacity(FIRST, _quota(FIRST, remaining_pct=0.0)) is False
