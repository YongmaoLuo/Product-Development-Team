"""
TDD tests for ``ProviderConcurrencyController``.

The five pinned contracts (PRD decision point 2 — reuse the dual-layer
rate limiting pattern, but as a standalone module decoupled from the
verification DAG):

  1. ``test_default_global_limit`` — env unset → global=5
  2. ``test_env_override`` — MAX_PARALLEL_TASKS=8 → global=8
  3. ``test_vendor-a_limit_5`` — vendor-a-pro 6 concurrent, 6th waits
  4. ``test_vendor-b_limit_2`` — vendor-b-pro 3 concurrent, 3rd waits
  5. ``test_unknown_provider_default_5`` — provider='other' → limit 5

The controller under test reads its config from env vars at
construction time, so each test uses ``monkeypatch.setenv`` /
``monkeypatch.delenv`` to set up the env state before constructing a
fresh controller. Tests do not share a controller (no fixture-level
state) so a leaked env var from a previous test cannot poison the
next.

The async tests use the ``@pytest.mark.asyncio`` marker (declared as
a marker in ``pytest.ini``); the strict default mode of
``pytest-asyncio`` is intentional — it keeps the asyncio tests
discoverable in CI.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from provider_concurrency import (
    DEFAULT_GLOBAL_LIMIT,
    DEFAULT_PROVIDER_LIMIT,
    ProviderConcurrencyController,
    ProviderConcurrencyError,
    select_provider_with_concurrency,
)


# ---------------------------------------------------------------------------
# 1. Default global limit when env vars are unset
# ---------------------------------------------------------------------------


def test_default_global_limit(monkeypatch):
    """When ``MAX_PARALLEL_TASKS`` is not set, ``global_limit == 5``.

    Boundary contract: ``MAX_PARALLEL_TASKS 未设 → 全局 5``. Also
    asserts that ``PROVIDER_LIMITS`` is empty when unset (no per-
    provider caps), so subsequent tests can rely on the "empty
    defaults" baseline.
    """
    monkeypatch.delenv("MAX_PARALLEL_TASKS", raising=False)
    monkeypatch.delenv("PROVIDER_LIMITS", raising=False)

    c = ProviderConcurrencyController()

    assert c.global_limit == 5
    # Sanity: the constant is in sync with the test pin.
    assert DEFAULT_GLOBAL_LIMIT == 5
    # And the empty PROVIDER_LIMITS yields an empty map.
    assert c.provider_limits == {}


# ---------------------------------------------------------------------------
# 2. Env var override for global limit
# ---------------------------------------------------------------------------


def test_env_override(monkeypatch):
    """``MAX_PARALLEL_TASKS=8`` → ``global_limit == 8``.

    Boundary contract: explicit env var wins over the hard-coded
    default. ``PROVIDER_LIMITS`` is left unset so the test isolates
    the global cap behaviour.
    """
    monkeypatch.setenv("MAX_PARALLEL_TASKS", "8")
    monkeypatch.delenv("PROVIDER_LIMITS", raising=False)

    c = ProviderConcurrencyController()

    assert c.global_limit == 8
    # Provider map is still empty (no per-provider caps).
    assert c.provider_limits == {}


# ---------------------------------------------------------------------------
# 3. Per-provider limit: vendor-a-pro=5 (6th acquire blocks)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendor_a_limit_5(monkeypatch):
    """vendor-a-pro=5: 6th concurrent ``acquire('vendor-a-pro')`` must block.

    Pins the per-provider cap contract. With a high global cap
    (``MAX_PARALLEL_TASKS=10``), the binding constraint is the
    per-provider cap of 5 — the 6th vendor-a-pro task must suspend until
    a paired ``release('vendor-a-pro')`` frees a slot. Boundary
    contract: ``重复 acquire 同 provider → 槽位累加`` (the 6th
    acquire is what makes the count exceed the cap, and it blocks).
    """
    monkeypatch.setenv("MAX_PARALLEL_TASKS", "10")
    monkeypatch.setenv("PROVIDER_LIMITS", '{"vendor-a-pro": 5}')

    c = ProviderConcurrencyController()
    assert c.global_limit == 10
    assert c.provider_limits == {"vendor-a-pro": 5}

    # Fill vendor-a-pro to its cap of 5 — these 5 acquires must all
    # complete immediately (no blocking yet, well within the 0.5s
    # wait budget).
    for _ in range(5):
        await asyncio.wait_for(c.acquire("vendor-a-pro"), timeout=0.5)

    # Sanity: per-provider count is 0 (all 5 slots are in flight).
    assert c.available_provider("vendor-a-pro") == 0
    # Global: 5 of 10 are in flight.
    assert c.available_global() == 5

    # 6th vendor-a-pro acquire: must block. We schedule it as a task and
    # let the event loop tick for a short while; if the task is
    # already done, the cap is wrong. 50ms is well above the
    # scheduler's tick budget on any reasonable platform, so a
    # non-done task here is proof of blocking.
    waiter = asyncio.create_task(c.acquire("vendor-a-pro"))
    await asyncio.sleep(0.05)
    assert not waiter.done(), (
        "6th vendor-a-pro acquire must block while 5 vendor-a-pro slots are in flight"
    )

    # Release one in-flight vendor-a-pro: the waiter must wake up within
    # a short timeout. If it doesn't, release() is broken.
    c.release("vendor-a-pro")
    await asyncio.wait_for(waiter, timeout=1.0)
    assert waiter.done(), "release() must wake exactly one waiter"

    # Drain: 5 originals + 1 the waiter consumed = 6 to release.
    for _ in range(5):
        c.release("vendor-a-pro")

    # Sanity: both pools back to full capacity — no leak.
    assert c.available_global() == 10
    assert c.available_provider("vendor-a-pro") == 5


# ---------------------------------------------------------------------------
# 4. Per-provider limit: vendor-b-pro=2 (3rd acquire blocks)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendor_b_limit_2(monkeypatch):
    """vendor-b-pro=2: 3rd concurrent ``acquire('vendor-b-pro')`` must block.

    Pins the per-provider cap contract for a tight (2) limit. The
    vendor-b-pro=2 case is the binding constraint in the canonical PRD
    fixture (vendor-b-pro is known to be flaky and we want to defend
    against 401 雪崩). A 3rd vendor-b-pro caller must suspend inside
    ``acquire()``; only a paired ``release('vendor-b-pro')`` can wake it.
    """
    monkeypatch.setenv("MAX_PARALLEL_TASKS", "10")
    monkeypatch.setenv("PROVIDER_LIMITS", '{"vendor-b-pro": 2}')

    c = ProviderConcurrencyController()
    assert c.provider_limits == {"vendor-b-pro": 2}

    # Fill vendor-b-pro to its cap of 2.
    for _ in range(2):
        await asyncio.wait_for(c.acquire("vendor-b-pro"), timeout=0.5)

    # Sanity: per-provider count is 0 (both slots in flight).
    assert c.available_provider("vendor-b-pro") == 0

    # 3rd vendor-b-pro acquire: must block.
    waiter = asyncio.create_task(c.acquire("vendor-b-pro"))
    await asyncio.sleep(0.05)
    assert not waiter.done(), (
        "3rd vendor-b-pro acquire must block while 2 vendor-b-pro slots are in flight"
    )

    # Release one in-flight vendor-b-pro: the waiter must wake up.
    c.release("vendor-b-pro")
    await asyncio.wait_for(waiter, timeout=1.0)
    assert waiter.done(), "release() must wake the vendor-b-pro waiter"

    # Drain: 2 originals + 1 the waiter consumed = 3 to release.
    c.release("vendor-b-pro")
    c.release("vendor-b-pro")

    # Sanity: both pools back to full capacity — no leak.
    assert c.available_global() == 10
    assert c.available_provider("vendor-b-pro") == 2


# ---------------------------------------------------------------------------
# 5. Default per-provider limit for unknown provider ('other' → 5)
# ---------------------------------------------------------------------------


def test_unknown_provider_default_5(monkeypatch):
    """Provider not in ``PROVIDER_LIMITS`` uses the default of 5.

    Boundary contract: ``provider 不在 PROVIDER_LIMITS → 默认 5``.
    The default is the module constant :data:`DEFAULT_PROVIDER_LIMIT`
    (a hard-coded 5, independent of the global cap), so the two
    layers can be tuned separately.

    Asserts:

      * ``available_provider('other')`` reports 5 (the implicit
        per-provider cap) before any acquire has run.
      * The hard-coded default constant matches the test pin.
    """
    monkeypatch.setenv("MAX_PARALLEL_TASKS", "10")
    monkeypatch.delenv("PROVIDER_LIMITS", raising=False)

    c = ProviderConcurrencyController()
    # 'other' is not in PROVIDER_LIMITS, so the implicit cap is
    # :data:`DEFAULT_PROVIDER_LIMIT` (5).
    assert c.available_provider("other") == 5
    # Sanity: the constant is in sync with the test pin.
    assert DEFAULT_PROVIDER_LIMIT == 5

    # The same applies to any other unknown provider string — the
    # default is keyed on absence from the map, not on the
    # provider name. "another-unknown" must also default to 5.
    assert c.available_provider("another-unknown") == 5
    # And known providers (in PROVIDER_LIMITS) still report their
    # own limit. Here PROVIDER_LIMITS is empty, so even a "known"
    # provider defaults to 5.
    assert c.available_provider("vendor-a-pro") == 5


# ---------------------------------------------------------------------------
# Bonus coverage — defensive: constructor validation
# ---------------------------------------------------------------------------


def test_constructor_rejects_non_positive_global(monkeypatch):
    """Explicit ``global_limit=0`` raises ``ProviderConcurrencyError``."""
    monkeypatch.delenv("MAX_PARALLEL_TASKS", raising=False)
    monkeypatch.delenv("PROVIDER_LIMITS", raising=False)
    with pytest.raises(ProviderConcurrencyError):
        ProviderConcurrencyController(global_limit=0)


def test_constructor_rejects_non_positive_provider_limit(monkeypatch):
    """A zero per-provider limit raises ``ProviderConcurrencyError``."""
    monkeypatch.delenv("MAX_PARALLEL_TASKS", raising=False)
    monkeypatch.delenv("PROVIDER_LIMITS", raising=False)
    with pytest.raises(ProviderConcurrencyError):
        ProviderConcurrencyController(provider_limits={"vendor-b-pro": 0})


def test_constructor_rejects_malformed_env_json(monkeypatch):
    """A non-JSON ``PROVIDER_LIMITS`` env var raises on construction."""
    monkeypatch.delenv("MAX_PARALLEL_TASKS", raising=False)
    monkeypatch.setenv("PROVIDER_LIMITS", "not-json")
    with pytest.raises(ProviderConcurrencyError):
        ProviderConcurrencyController()


def test_constructor_rejects_non_integer_env(monkeypatch):
    """A non-integer ``MAX_PARALLEL_TASKS`` env var raises on construction."""
    monkeypatch.setenv("MAX_PARALLEL_TASKS", "not-a-number")
    monkeypatch.delenv("PROVIDER_LIMITS", raising=False)
    with pytest.raises(ProviderConcurrencyError):
        ProviderConcurrencyController()


# ---------------------------------------------------------------------------
# Chain-order selection
# ---------------------------------------------------------------------------
# The selector applies no ranking policy of its own: the ORDER it is handed
# is the optimizer's chain (``provider-order.json``), whose rule engine
# already applied any peak-hour demotion. These tests pin the capacity walk.


@pytest.mark.asyncio
async def test_select_provider_returns_the_chain_head(monkeypatch):
    monkeypatch.setenv("MAX_PARALLEL_TASKS", "10")
    monkeypatch.setenv("PROVIDER_LIMITS", '{"vendor-b-pro": 2, "vendor-a-pro": 5}')

    controller = ProviderConcurrencyController()
    now = datetime(2026, 6, 15, 15, 0, 0)

    selected = select_provider_with_concurrency(
        ["vendor-b-pro", "vendor-a-pro"], controller, now
    )
    assert selected == "vendor-b-pro"


@pytest.mark.asyncio
async def test_select_provider_falls_through_a_saturated_head(monkeypatch):
    """A provider whose concurrency is exhausted is skipped; the walk moves on.

    This is the mechanism that makes a rule-driven demotion effective: the
    optimizer puts the avoided provider LAST in the chain, and the walk
    simply takes the first entry with capacity.
    """
    monkeypatch.setenv("MAX_PARALLEL_TASKS", "10")
    monkeypatch.setenv("PROVIDER_LIMITS", '{"vendor-b-pro": 1, "vendor-a-pro": 5}')

    controller = ProviderConcurrencyController()
    await controller.acquire("vendor-b-pro")  # head is now saturated
    now = datetime(2026, 6, 15, 15, 0, 0)

    selected = select_provider_with_concurrency(
        ["vendor-b-pro", "vendor-a-pro"], controller, now
    )
    assert selected == "vendor-a-pro"


@pytest.mark.asyncio
async def test_select_provider_returns_none_when_every_provider_is_saturated(
    monkeypatch,
):
    """No capacity anywhere → ``None`` (the caller falls back to parent).

    The global cap must be >= the number of acquires below, otherwise the
    second ``acquire`` blocks forever on the global semaphore.
    """
    monkeypatch.setenv("MAX_PARALLEL_TASKS", "2")  # room for both acquires
    monkeypatch.setenv("PROVIDER_LIMITS", '{"vendor-b-pro": 1, "vendor-a-pro": 1}')

    controller = ProviderConcurrencyController()
    await controller.acquire("vendor-b-pro")
    await controller.acquire("vendor-a-pro")
    now = datetime(2026, 6, 15, 15, 0, 0)

    assert (
        select_provider_with_concurrency(
            ["vendor-b-pro", "vendor-a-pro"], controller, now
        )
        is None
    )
