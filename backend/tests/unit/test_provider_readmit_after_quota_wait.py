"""A provider excluded for a non-quota reason comes back before a wait.

The failure handler writes a provider into ``excluded`` for whatever
reason the failure carried, and the exclusion set rides down the retry
chain: the candidate set is rebuilt from the chain, the exclusion set
and the park, and nothing else. So a provider excluded for a reason
that has nothing to do with quota — a transient API error, say — stays
out of the candidate set for the whole call.

That is invisible while the fleet still has a candidate. It is not
invisible when the remaining candidates all go into a quota wait: the
call waits for budget to come back while a provider that still has
budget sits outside the pool, excluded for a reason that expired the
moment the error did.

The readmission is a pure function, and it says *once*. A provider that
has been readmitted once is not readmitted again, so the exclusion
cannot be used to keep a provider out and then let it back in
indefinitely; and a provider whose own reading says it is shut, or
whose park has not expired, is not readmitted at all — the wait is
about the whole fleet, and a provider that cannot serve does not
become servable by being let back into the pool.

These tests cover the decision only. Nothing here dispatches anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool  # noqa: E402
import provider_order  # noqa: E402

CHAIN = ["Vendor A", "Vendor B", "Vendor C"]


def _open(name: str, remaining_pct: float = 22.0):
    """A reading that says the provider still has budget."""
    return provider_order.ProviderQuota(
        provider=name,
        shape=provider_order.SHAPE_ROLLING,
        remaining_pct=remaining_pct,
        exhausted=False,
    )


def _shut(name: str):
    """A reading that says the provider has no budget left."""
    return provider_order.ProviderQuota(
        provider=name,
        shape=provider_order.SHAPE_ROLLING,
        remaining_pct=0.0,
        exhausted=True,
    )


def test_non_quota_excluded_provider_is_readmitted_once():
    """The provider comes back — once.

    The fleet is waiting for quota, so the only candidate left is one
    that was excluded for a reason that has nothing to do with quota.
    It comes back, and it comes back for good: the second call does
    not readmit it again, so the exclusion still stands.
    """
    excluded = {"Vendor A"}
    readmitted = set()
    quota_map = {
        "Vendor A": _open("Vendor A"),
        "Vendor B": _shut("Vendor B"),
        "Vendor C": _shut("Vendor C"),
    }

    admitted = coding_tool._readmit_providers_for_quota_wait(
        excluded, CHAIN, quota_map, readmitted,
    )

    assert admitted == ["Vendor A"], (
        "the only provider with budget left was excluded for a non-quota "
        "reason and did not come back"
    )
    assert "Vendor A" not in excluded, (
        "the readmitted provider is still excluded, so it stays out of the "
        "candidate set"
    )
    assert readmitted == {"Vendor A"}

    # The second pass: the provider is excluded again (by whatever put
    # it there) and must NOT come back a second time.
    excluded.add("Vendor A")
    admitted_again = coding_tool._readmit_providers_for_quota_wait(
        excluded, CHAIN, quota_map, readmitted,
    )

    assert admitted_again == [], (
        "a provider that was already readmitted must not be readmitted again"
    )
    assert "Vendor A" in excluded


def test_parked_provider_is_not_readmitted():
    """A provider in cooldown is not let back in.

    The park is the ledger of "this provider is not to be used right
    now", and it expires by itself. Reading a provider back in while
    its park stands would undo the cooldown.
    """
    excluded = {"Vendor A"}
    readmitted = set()
    quota_map = {"Vendor A": _open("Vendor A")}

    admitted = coding_tool._readmit_providers_for_quota_wait(
        excluded,
        CHAIN,
        quota_map,
        readmitted,
        is_parked=lambda name: name == "Vendor A",
    )

    assert admitted == []
    assert excluded == {"Vendor A"}
    assert readmitted == set()


def test_exhausted_provider_is_not_readmitted():
    """A provider whose own reading says it is shut stays out.

    The wait exists because the budget ran out. A provider that is out
    of budget is exactly the thing the wait is about, so reading it
    back in would make the call wait on the thing it is already
    waiting for.
    """
    excluded = {"Vendor A"}
    readmitted = set()
    quota_map = {"Vendor A": _shut("Vendor A")}

    admitted = coding_tool._readmit_providers_for_quota_wait(
        excluded, CHAIN, quota_map, readmitted,
    )

    assert admitted == []
    assert excluded == {"Vendor A"}
    assert readmitted == set()


def test_missing_reading_is_not_a_verdict():
    """A provider with no reading is readmitted.

    "No reading" is the absence of a fact, not a verdict. The reading
    is what says a provider is shut; without one there is nothing to
    stand on, and a provider dropped out of the pool on no evidence
    at all is a provider dropped out of the pool for no reason.
    """
    excluded = {"Vendor A"}
    readmitted = set()
    quota_map = {"Vendor B": _shut("Vendor B")}

    admitted = coding_tool._readmit_providers_for_quota_wait(
        excluded, CHAIN, quota_map, readmitted,
    )

    assert admitted == ["Vendor A"]
    assert "Vendor A" not in excluded

    # The same holds when there is no reading at all.
    excluded = {"Vendor A"}
    readmitted = set()
    admitted = coding_tool._readmit_providers_for_quota_wait(
        excluded, CHAIN, {}, readmitted,
    )

    assert admitted == ["Vendor A"]
    assert "Vendor A" not in excluded


def test_provider_outside_chain_is_not_readmitted():
    """Only a provider the chain names can come back.

    The chain is the pool. A name the chain does not carry is not in
    the pool, so letting it back in would put a provider into the
    candidate set that the caller never asked for.
    """
    excluded = {"Vendor Z"}
    readmitted = set()
    quota_map = {"Vendor Z": _open("Vendor Z")}

    admitted = coding_tool._readmit_providers_for_quota_wait(
        excluded, CHAIN, quota_map, readmitted,
    )

    assert admitted == []
    assert excluded == {"Vendor Z"}
    assert readmitted == set()

    # Nothing excluded, nothing to readmit.
    admitted = coding_tool._readmit_providers_for_quota_wait(
        set(), CHAIN, quota_map, set(),
    )

    assert admitted == []


def test_chain_order_is_kept():
    """The readmitted names come back in the chain's order.

    The chain is the order the caller wants tried in, so the readmitted
    names follow it rather than whatever order a set happens to hold.
    """
    excluded = {"Vendor A", "Vendor C"}
    readmitted = set()
    quota_map = {
        "Vendor A": _open("Vendor A"),
        "Vendor B": _shut("Vendor B"),
        "Vendor C": _open("Vendor C"),
    }

    admitted = coding_tool._readmit_providers_for_quota_wait(
        excluded, CHAIN, quota_map, readmitted,
    )

    assert admitted == ["Vendor A", "Vendor C"]
    assert excluded == set()
    assert readmitted == {"Vendor A", "Vendor C"}


def test_chain_order_is_not_set_order():
    """A chain that is not alphabetical is still followed in its order."""
    chain = ["Vendor C", "Vendor A", "Vendor B"]
    excluded = {"Vendor A", "Vendor C"}
    readmitted = set()
    quota_map = {
        "Vendor A": _open("Vendor A"),
        "Vendor C": _open("Vendor C"),
    }

    admitted = coding_tool._readmit_providers_for_quota_wait(
        excluded, chain, quota_map, readmitted,
    )

    assert admitted == ["Vendor C", "Vendor A"], (
        "the readmitted names follow the chain's order, not the order a "
        "set happens to iterate in"
    )
