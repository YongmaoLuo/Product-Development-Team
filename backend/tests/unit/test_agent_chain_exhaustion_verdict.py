"""The exhaustion verdict is about the WHOLE chain, not the last provider.

2026-10-27 — "the provider I tried" is not "the providers I could have tried"
-----------------------------------------------------------------------
When the capacity-wait budget runs out, the wait used to raise
``QuotaWaitExhausted(providers=self._capacity_wait_providers())`` — and
``_capacity_wait_providers()`` answers a much narrower question than the
one the marker asks. It reports **the provider the last refused call
happened to go to**. One name.

But the reader of that marker asks "which providers could have served
this call?", and the answer to that is the whole chain the scene routes
to. A scene with three providers, where only the last one refused, has
not run out of providers — it has run out of *one* provider, and the two
facts have to stay apart:

* the chain is out      → the run waits for a window to reopen;
* one member is out     → the run still has somewhere to go.

So the exhaustion verdict is now asked of the chain, using the same
predicate the dispatch loop uses to decide whether a provider may be
dispatched to at all (:func:`chain_has_dispatchable_provider`): a member
is out of capacity when it is parked, when its live reading says the
window is empty, or when its quota-scaled cap comes out at nothing.

What these tests pin:

  * a chain that still has a dispatchable member does NOT raise — the
    wait says so in the log and makes one more call, the call that
    decides it;
  * a chain where every member is out raises, and the exception names
    the WHOLE chain;
  * a chain nobody could resolve (routing failed) keeps the old answer:
    the provider the call went to;
  * the extra call is one call, not a loop — a chain that keeps looking
    alive still ends in an exhaustion, with the whole chain named;
  * a budget of zero ("escalate now, never wait") keeps the old verdict
    exactly: nothing was waited out, so there is nothing to re-judge.

Nothing here touches the network: the readings are built directly, the
routing resolver and the quota reader are stubbed, and the park list is
a plain :class:`ProviderCooldown`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import provider_routing  # noqa: E402
from capacity_wait import QuotaWaitExhausted  # noqa: E402
from coding_tool import ApiError  # noqa: E402
from dynamic_provider_concurrency import (  # noqa: E402
    ProviderCooldown,
    set_shared_cooldown,
)
from provider_order import ProviderQuota  # noqa: E402


#: The chain the scene routes to. Three members, so "the last provider"
#: and "the chain" cannot be mistaken for one another.
CHAIN = ["Alpha", "Bravo", "Charlie"]

#: The provider the refused call went to — the OLD answer, kept for the
#: routing-failure case.
LAST_PROVIDER = "vendor-a"

#: The log event that says the chain still had somebody on it.
NOT_EXHAUSTED_EVENT = "provider_chain_not_exhausted_yet"

#: The budget the wait is given in these tests: one second, so the
#: fake clock is past it the moment it is read. A budget of zero would
#: mean "escalate now" and would take the old path.
BUDGET_SEC = "1"


def _quota(provider: str, **kwargs) -> ProviderQuota:
    """A reading for ``provider`` with the given fields overridden."""
    fields = {"provider": provider, "remaining_pct": 100.0}
    fields.update(kwargs)
    return ProviderQuota(**fields)


class _Tool:
    """The part of the coding tool the chain resolution reads."""

    def __init__(self, scene=None, provider=LAST_PROVIDER):
        self.scene = scene
        self.current_call_provider = provider


class _Logger:
    """A logger that keeps the events it was handed."""

    def __init__(self):
        self.warnings = []
        self.errors = []
        self.infos = []

    def warning(self, event, message, **kwargs):
        self.warnings.append((event, message, kwargs))

    def error(self, event, message, **kwargs):
        self.errors.append((event, message, kwargs))

    def info(self, event, message, **kwargs):
        self.infos.append((event, message, kwargs))

    def events(self, kind="warnings"):
        """The event names of one kind of log line."""
        return [entry[0] for entry in getattr(self, kind)]


class _Clock:
    """A clock that is past the budget the moment it is read.

    The wait reads the clock twice — once to start, once to see how
    long it has waited — so the second read is what puts it over the
    budget without the test waiting for a real second to pass.
    """

    def __init__(self):
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return 0.0 if self.reads == 1 else 1000.0


def _make_agent(tool, logger=None):
    """An agent wired with only what the wait touches."""
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.coding_tool = tool
    agent.logger = logger
    agent._capacity_wait_clock = _Clock()
    agent._capacity_wait_sleep = lambda seconds: None
    return agent


def _task():
    """A task object good enough for the wait's log line."""
    from task import SubTask

    return SubTask(
        id="14-2",
        title="implement the thing",
        description="implement the thing",
        test_command="exit 1",
        status="pending",
        project_dir=".",
        files_to_modify=["changed.py"],
        model_type="medium",
    )


def _stub_chain(monkeypatch, chain):
    """Make the routing resolver answer ``chain``."""
    def _fake(scene, *args, **kwargs):
        return list(chain)

    monkeypatch.setattr(provider_routing, "resolve_provider_chain", _fake)


def _stub_quota_map(monkeypatch, quota_map):
    """Make the live quota reader answer ``quota_map``."""
    import provider_order

    def _fake(*args, **kwargs):
        return dict(quota_map)

    monkeypatch.setattr(provider_order, "load_provider_quota_map", _fake)


@pytest.fixture
def park_list():
    """The park list, emptied again when the test is done."""
    set_shared_cooldown(ProviderCooldown())
    try:
        yield
    finally:
        set_shared_cooldown(None)


def _refusing_call(calls):
    """A call that refuses every time, and counts itself."""
    def _call():
        calls.append(len(calls))
        raise ApiError("quota exceeded", status="429")

    return _call


# ---------------------------------------------------------------------------
# A chain that still has somebody on it
# ---------------------------------------------------------------------------


def test_dispatchable_chain_does_not_raise(monkeypatch, park_list):
    """A chain with a member left on it must not be declared exhausted.

    ``Bravo`` reads 90% left, so the scene still has a provider that
    could serve this call: the wait budget running out is not the
    chain running out.  The wait says so in the log and makes one more
    call — and that call is what decides it.
    """
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", BUDGET_SEC)
    _stub_chain(monkeypatch, CHAIN)
    _stub_quota_map(monkeypatch, {
        "Alpha": _quota("Alpha", remaining_pct=0.0),
        "Bravo": _quota("Bravo", remaining_pct=90.0),
        "Charlie": _quota("Charlie", remaining_pct=0.0),
    })
    from dynamic_provider_concurrency import get_shared_cooldown

    get_shared_cooldown().park(
        "Alpha", seconds=300.0, reason="capacity error",
    )
    logger = _Logger()
    agent = _make_agent(_Tool(scene="execution"), logger)

    calls = []

    def _call():
        calls.append(len(calls))
        if len(calls) == 1:
            raise ApiError("quota exceeded", status="429")
        return "the answer"

    result = agent._query_with_capacity_wait(
        _call, _task(), (1, 2), scene="execution",
    )

    assert result == "the answer", (
        "the chain still had a member on it, so the wait must not give up"
    )
    assert len(calls) == 2, (
        f"the wait must make exactly one more call; got {len(calls)}"
    )
    assert NOT_EXHAUSTED_EVENT in logger.events(), (
        f"the wait must say the chain was not exhausted; got "
        f"{logger.events()!r}"
    )


# ---------------------------------------------------------------------------
# A chain where every member is out
# ---------------------------------------------------------------------------


def test_all_members_unavailable_raises_with_whole_chain(
    monkeypatch, park_list,
):
    """Every member out → the wait is over, and it names the whole chain.

    The providers the exception names are the ones the scene could
    have used, not the one that happened to refuse last: "the chain is
    out" and "that one provider is out" are different facts, and only
    the whole chain can tell them apart.
    """
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", BUDGET_SEC)
    _stub_chain(monkeypatch, CHAIN)
    # Each member is out for one of the three reasons a provider stops
    # being able to serve: parked, no budget left, or a cap that
    # scales to nothing.
    _stub_quota_map(monkeypatch, {
        "Alpha": _quota("Alpha", remaining_pct=0.0),
        "Bravo": _quota("Bravo", remaining_pct=100.0, exhausted=True),
        "Charlie": _quota("Charlie", remaining_pct=0.0),
    })
    from dynamic_provider_concurrency import get_shared_cooldown

    get_shared_cooldown().park(
        "Alpha", seconds=300.0, reason="capacity error",
    )
    logger = _Logger()
    agent = _make_agent(_Tool(scene="execution"), logger)

    calls = []

    with pytest.raises(QuotaWaitExhausted) as excinfo:
        agent._query_with_capacity_wait(
            _refusing_call(calls), _task(), (1, 2), scene="execution",
        )

    assert excinfo.value.providers == CHAIN, (
        f"the exhaustion must name the whole chain; got "
        f"{excinfo.value.providers!r}"
    )
    assert len(calls) == 1, (
        f"a chain with nobody left on it must not buy another call; got "
        f"{len(calls)}"
    )
    assert NOT_EXHAUSTED_EVENT not in logger.events(), (
        "the chain WAS exhausted — there is nothing to warn about"
    )


# ---------------------------------------------------------------------------
# A chain nobody could resolve
# ---------------------------------------------------------------------------


def test_empty_chain_falls_back_to_last_provider(monkeypatch, park_list):
    """A routing failure keeps the old answer: the provider that refused.

    An empty chain is "nobody answered", not "everybody is out".  The
    older answer — the provider the call went to — is the only one
    that says anything, so it stands.
    """
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", BUDGET_SEC)
    _stub_chain(monkeypatch, [])
    _stub_quota_map(monkeypatch, {})
    logger = _Logger()
    agent = _make_agent(_Tool(scene="execution"), logger)

    calls = []

    with pytest.raises(QuotaWaitExhausted) as excinfo:
        agent._query_with_capacity_wait(
            _refusing_call(calls), _task(), (1, 2), scene="execution",
        )

    assert excinfo.value.providers == [LAST_PROVIDER], (
        f"an unresolvable chain must fall back to the provider the call "
        f"went to; got {excinfo.value.providers!r}"
    )


# ---------------------------------------------------------------------------
# The edges around those three
# ---------------------------------------------------------------------------


def test_extra_call_is_bounded_to_one(monkeypatch, park_list):
    """The extra call is one call, not a loop.

    A chain that still looks alive must not keep the wait alive: the
    wait makes one more call, and that call failing ends the wait.
    """
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", BUDGET_SEC)
    _stub_chain(monkeypatch, CHAIN)
    _stub_quota_map(monkeypatch, {
        "Alpha": _quota("Alpha", remaining_pct=90.0),
        "Bravo": _quota("Bravo", remaining_pct=90.0),
        "Charlie": _quota("Charlie", remaining_pct=90.0),
    })
    logger = _Logger()
    agent = _make_agent(_Tool(scene="execution"), logger)

    calls = []

    with pytest.raises(QuotaWaitExhausted) as excinfo:
        agent._query_with_capacity_wait(
            _refusing_call(calls), _task(), (1, 2), scene="execution",
        )

    assert len(calls) == 2, (
        f"the extra call is capped at one; got {len(calls)} calls"
    )
    assert excinfo.value.providers == CHAIN


def test_zero_budget_keeps_the_old_verdict(monkeypatch, park_list):
    """A budget of zero means "escalate now" — the old answer stands.

    Nothing was waited out, so there is nothing to re-judge: the
    verdict is the one the wait has always given.
    """
    monkeypatch.setenv("PDT_CAPACITY_WAIT_MAX_SEC", "0")
    _stub_chain(monkeypatch, CHAIN)
    _stub_quota_map(monkeypatch, {
        "Alpha": _quota("Alpha", remaining_pct=0.0),
        "Bravo": _quota("Bravo", remaining_pct=90.0),
        "Charlie": _quota("Charlie", remaining_pct=0.0),
    })
    logger = _Logger()
    agent = _make_agent(_Tool(scene="execution"), logger)

    calls = []

    with pytest.raises(QuotaWaitExhausted) as excinfo:
        agent._query_with_capacity_wait(
            _refusing_call(calls), _task(), (1, 2), scene="execution",
        )

    assert excinfo.value.providers == [LAST_PROVIDER], (
        f"a zero budget must not re-judge the chain; got "
        f"{excinfo.value.providers!r}"
    )
    assert len(calls) == 1
