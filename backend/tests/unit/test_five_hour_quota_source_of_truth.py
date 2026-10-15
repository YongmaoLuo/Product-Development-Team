"""Which source decides the 5-hour remaining quota — and the legacy leg is not dead.

Task I9 asked whether the ``provider-order.json`` fallback in
:func:`provider_order.load_provider_5h_usage` could be deleted as dead.
The evidence says it cannot be deleted *as dead*, and these tests pin
the part that makes it undeletable:

* it has **no producer**. Nothing in this repository, and nothing in
  the optimizer that writes the file, ever emits
  ``providers.<id>.five_hour_remaining_pct``.
* it is nevertheless **executed on every dispatch**, by three
  production modules, and its result is threaded into
  :func:`provider_headroom_pct` as the legacy leg.

So it is a *legacy leg with no producer*, not an unreachable branch —
a different thing. "Nothing writes the file" does not make the reader
dead; it makes it read empty. The reader is still on a live path, and
its *result* is still live.

What this file does and does not pin
------------------------------------
It pins **where the number comes from** — the live
``GET <base>/api/usage`` reading is what carries the 5-hour window into
the concurrency cap, and the legacy file reader is a reachable
fallback rather than dead code. Nothing here reaches the network — the
payloads are the shapes the reader is documented against.

It deliberately does **not** pin *which* window scales the cap when a
row carries two. :func:`_usage_row` emits the burst tier alone (see its
docstring), so ``max(five_hour_used, weekly_used)`` collapses to
``five_hour_used`` and every assertion below holds identically whether
the reader scales by the burst window alone or by the worse of the two.
That question — the week gates a dispatch but does not shrink the cap —
is pinned in ``tests/unit/test_provider_quota_dispatch.py``, by
``test_the_weekly_window_does_not_shrink_the_cap`` and its counterweight
``test_a_week_with_no_burst_window_beside_it_says_nothing``. This file
points at them rather than restating them.
"""

from __future__ import annotations

import ast
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import provider_order  # noqa: E402
from dynamic_provider_concurrency import (  # noqa: E402
    ActiveConcurrencyTracker,
    compute_dynamic_limit,
    provider_headroom_pct,
    select_provider_with_dynamic_capacity,
)

#: The hermetic ``provider_capacity.yaml`` in ``conftest`` points every
#: provider at the documented default, so ``DEFAULT_CAP`` is what any
#: name below resolves to. Pinned here rather than imported so a change
#: to that default has to be made deliberately in two places.
DEFAULT_CAP = 5


def _now():
    return datetime.now(timezone.utc)


def _usage_row(provider, five_hour_used):
    """A ``/api/usage`` provider row metered on the 5-hour window alone.

    Only the burst tier is present. That is deliberate: with no weekly
    tier, the reading's headroom is the burst window's under both the
    committed reader and the one an in-flight change to
    :func:`provider_order._quota_from_record` alters, so an assertion
    written against this row survives that change instead of pinning
    a number that is about to move.
    """
    base = _now()
    return {
        "provider": provider,
        "app_type": "claude",
        "tiers": [
            {
                "name": "five_hour",
                "used_percent": five_hour_used,
                "remaining": round(100.0 - five_hour_used, 2),
                "total": 100.0,
                "unit": "%",
                "resets_at": (base + timedelta(minutes=60)).isoformat(),
            },
        ],
        "updated_at": base.isoformat(),
        "success": True,
    }


def _quota_for(provider, five_hour_used):
    return provider_order._quota_from_record(
        provider, _usage_row(provider, five_hour_used), _now()
    )


# ---------------------------------------------------------------------------
# 1. The live reading is the one that decides the number
# ---------------------------------------------------------------------------


def test_live_reading_carries_the_5h_window_into_the_cap():
    """The number that decides the cap, from the production entry point.

    ``select_provider_with_dynamic_capacity`` is what ``coding_tool``
    and the dispatch path call. A provider 20% into its 5-hour window
    has 80% of its cap left, and that must be visible at the entry
    point — not only inside a helper.
    """
    quota = _quota_for("Example A", five_hour_used=20.0)
    assert quota.remaining_pct == 80.0

    usage_map: dict = {}
    quota_map = {"Example A": quota}
    tracker = ActiveConcurrencyTracker()

    cap = compute_dynamic_limit(
        "Example A", provider_headroom_pct("Example A", usage_map, quota_map)
    )
    assert cap == 4  # 80% of the default cap of 5

    # Sit one below the cap: the provider is still selectable.
    for _ in range(cap - 1):
        assert tracker.try_acquire("Example A", cap) is True
    assert (
        select_provider_with_dynamic_capacity(
            ["Example A"], tracker, usage_map, quota_map=quota_map
        )
        == "Example A"
    )

    # Sit exactly at the cap: the live number now closes the door.
    assert tracker.try_acquire("Example A", cap) is True
    assert (
        select_provider_with_dynamic_capacity(
            ["Example A"], tracker, usage_map, quota_map=quota_map
        )
        is None
    )


def test_live_number_wins_and_the_legacy_map_is_not_consulted():
    """When the reading carries a number, the legacy map cannot change it.

    This is the invariant a change to the legacy reader would have to
    preserve: ``provider_headroom_pct`` returns at the live reading and
    never reaches the ``usage_map`` lookup. An implementation that
    consulted the legacy map first would show up here.
    """
    quota = _quota_for("Example A", five_hour_used=20.0)
    assert (
        provider_headroom_pct("Example A", {"Example A": 5.0}, {"Example A": quota})
        == 80.0
    )
    assert (
        provider_headroom_pct("Example A", {"Example A": 100.0}, {"Example A": quota})
        == 80.0
    )


# ---------------------------------------------------------------------------
# 2. The legacy leg is a *reachable* branch, not a dead one
# ---------------------------------------------------------------------------


def test_a_reading_with_no_number_hands_the_decision_to_the_legacy_map():
    """The branch the file reader feeds is live — and reachable today.

    A row CC Switch could not meter yields ``remaining_pct is None``.
    That is not an exotic shape: it is what ``success: false`` gives,
    and what a burst window with no usable percentage gives. When the
    live reading has no number, ``provider_headroom_pct`` falls through
    to the legacy map — so removing the legacy reader would change the
    answer for every such provider, from whatever the file said to a
    flat 100.
    """
    meterless = provider_order._quota_from_record(
        "Example A", {"provider": "Example A", "success": False}, _now()
    )
    assert meterless.remaining_pct is None

    # The legacy map is consulted, and it decides.
    assert (
        provider_headroom_pct("Example A", {"Example A": 40.0}, {"Example A": meterless})
        == 40.0
    )
    # With no legacy entry either, the last resort is a full window.
    assert provider_headroom_pct("Example A", {}, {"Example A": meterless}) == 100.0
    # And with no live reading at all, the same two answers.
    assert provider_headroom_pct("Example A", {"Example A": 40.0}, None) == 40.0
    assert provider_headroom_pct("Example A", {}, None) == 100.0


# ---------------------------------------------------------------------------
# 3. What the legacy reader does today
# ---------------------------------------------------------------------------


def test_missing_file_yields_an_empty_map_and_says_so(
    tmp_path, monkeypatch, caplog
):
    """The deployed state: the file is absent, and the reader says so.

    Eight ``provider-order.json not found ... (reason=file_missing)``
    lines appear in ``pdt-backend.log`` between 2026-10-12 and
    2026-10-14. This is the behaviour behind them.
    """
    monkeypatch.setenv("PDT_PROVIDER_USAGE_FILE", str(tmp_path / "absent.json"))
    provider_order.cache_clear()
    with caplog.at_level(logging.WARNING, logger="provider_order"):
        assert provider_order.load_provider_5h_usage() == {}
    assert "provider-order.json not found" in caplog.text
    assert "reason=file_missing" in caplog.text


@pytest.mark.parametrize(
    "module_name",
    ["agent", "coding_tool", "verification_agent"],
)
def test_production_modules_still_call_the_legacy_reader(module_name):
    """Removal would be a cross-module change, not a local one.

    Parsed rather than imported so a module with import-time side
    effects is not dragged into the suite to answer a question about
    its source.
    """
    tree = ast.parse((BACKEND_DIR / f"{module_name}.py").read_text(encoding="utf-8"))
    calls_it = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "load_provider_5h_usage"
        for node in ast.walk(tree)
    )
    assert calls_it, (
        f"{module_name}.py no longer calls load_provider_5h_usage — "
        "the legacy leg has lost a production caller, which is the only "
        "thing that would have made it safe to remove"
    )
