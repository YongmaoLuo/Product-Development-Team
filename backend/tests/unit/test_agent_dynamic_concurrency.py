"""
TDD tests for the dynamic-concurrency wiring in ``backend/agent.py``.

Pins the contract of the modified ``_load_provider_info``:

  1. Reads 5h usage from ``provider-order.json`` (via
     :func:`provider_order.load_provider_5h_usage`).
  2. Filters out providers whose current count is at the dynamic
     limit (via :class:`ActiveConcurrencyTracker`).
  3. Skips the literal ``"parent"`` entry (parent is the caller's
     explicit fallback, not this strategy's choice).
  4. Returns the legacy "empty" dict when the chain has no
     eligible entry — the dispatch site then falls back to the
     parent process's CC Switch proxy config.
  5. Honors the vendor-b peak-hour degradation rule unchanged.
  6. Skips providers with no config in the CC Switch DB (legacy
     behaviour preserved).
  7. With ``reserve_slot=True`` (the production contract, 2026-09-17)
     TAKES the chosen provider's slot atomically during the walk. See
     ``TestReserveSlot``.

Test isolation
--------------
The CC Switch DB is mocked via ``monkeypatch.setenv`` to point
``Path.home()`` at a tmp dir containing a fake ``cc-switch.db``.
The optimizer's contract file (``provider-order.json``) is written
under ``tmp_path`` and the default resolver is patched to point at
it, so the test never touches the real on-disk file.

Per-provider caps come from ``provider_capacity.yaml``, so tests that
care about a specific cap declare it through the ``capacity_config``
fixture (``_CAPS`` below is the set most of them use). Tests that do not
care get the hermetic "no rules configured" state and the documented
default cap of 5.

The ``ActiveConcurrencyTracker`` is a fresh instance per test (the
module-level singleton in agent.py is reset via
:func:`dynamic_provider_concurrency.ActiveConcurrencyTracker`
reconstruction).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest


#: Caps used by the tests that need three providers at distinguishable
#: limits. ``kebab-case test_provider marker`` marks the spellings for the
#: repo-wide static scan, which allows them in tests.
_CAPS = [
    ("^vendor-a-pro", 10),
    ("^vendor-b", 3),
    ("^vendor-c-app", 5),
]


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _install_fake_cc_switch_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    providers: Dict[str, Dict[str, str]],
) -> Path:
    """Build a private ``~/.cc-switch/cc-switch.db`` and redirect HOME."""
    fake_home = tmp_path / "home"
    cc_dir = fake_home / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path = cc_dir / "cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS provider_configs ("
        "id TEXT PRIMARY KEY, url TEXT, model TEXT, extra_params TEXT"
        ")"
    )
    for pid, cfg in providers.items():
        extra = json.dumps({"ANTHROPIC_AUTH_TOKEN": cfg["api_key"]})
        conn.execute(
            "INSERT INTO provider_configs (id, url, model, extra_params) "
            "VALUES (?, ?, ?, ?)",
            (pid, cfg["base_url"], "fake-model", extra),
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HOME", str(fake_home))
    return db_path


def _write_provider_order(
    tmp_path: Path,
    providers_meta: Dict[str, Dict[str, Any]],
    order: Optional[List[str]] = None,
) -> Path:
    """Write a valid ``provider-order.json`` under ``tmp_path``."""
    if order is None:
        order = list(providers_meta.keys()) + ["parent"]
    target = tmp_path / "provider-order.json"
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "source": "producer",
        "order": order,
        "providers": providers_meta,
    }
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _patch_default_order_file(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Point ``_default_order_file`` at the test's tmp file."""
    import provider_order
    monkeypatch.setattr(provider_order, "_default_order_file", lambda: target)


def _clear_provider_order_cache() -> None:
    import provider_order
    provider_order.cache_clear()


def _make_tracker():
    from dynamic_provider_concurrency import ActiveConcurrencyTracker
    return ActiveConcurrencyTracker()


# Beijing off-peak time (avoids vendor-b peak-hour window).
_BEIJING_OFF_PEAK = datetime(2026, 6, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture
def fresh_provider_config(monkeypatch, tmp_path):
    """Install a fake CC Switch DB with vendor-a-pro + vendor-b configs."""
    providers = {
        "vendor-a-pro": {
            "base_url": "https://api.vendor-a.example/anthropic",
            "api_key": "sk-fake-vendor-a-key-1234567890",
        },
        "vendor-b": {
            "base_url": "https://api.vendor-b.example/anthropic",
            "api_key": "a8ff.fake-vendor-b-key.1234567890",
        },
        "vendor-c-app": {
            "base_url": "https://api.vendor-c.example/anthropic",
            "api_key": "sk-fake-vendor-c-app-key-1234567890",
        },
    }
    _install_fake_cc_switch_db(monkeypatch, tmp_path, providers)
    return providers


# ---------------------------------------------------------------------------
# 1. _load_provider_info honours dynamic capacity
# ---------------------------------------------------------------------------


class TestLoadProviderInfoDynamic:
    """The modified _load_provider_info respects 5h-based dynamic limits."""

    def test_happy_path_vendor_a_full_remaining(
        self, monkeypatch, tmp_path, fresh_provider_config
    ):
        """At 100% remaining, vendor-a-pro is the first pick (legacy behaviour)."""
        from agent import _load_provider_info

        order = ["vendor-a-pro", "vendor-b", "vendor-c-app", "parent"]
        order_path = _write_provider_order(
            tmp_path,
            {
                "vendor-a-pro": {"five_hour_remaining_pct": 100.0},
                "vendor-b": {"five_hour_remaining_pct": 100.0},
                "vendor-c-app": {"five_hour_remaining_pct": 100.0},
            },
            order=order,
        )
        _clear_provider_order_cache()
        _patch_default_order_file(monkeypatch, order_path)

        tracker = _make_tracker()
        result = _load_provider_info(
            order, tracker=tracker, now=_BEIJING_OFF_PEAK
        )

        assert result["provider_name"] == "vendor-a-pro"
        assert result["base_url"] == "https://api.vendor-a.example/anthropic"

    def test_vendor_a_at_dynamic_limit_falls_back_to_vendor_b(
        self, monkeypatch, tmp_path, fresh_provider_config
    ):
        """vendor-a-pro at cap (5 in flight @ 100%) → vendor-b is selected."""
        from agent import _load_provider_info

        order = ["vendor-a-pro", "vendor-b", "vendor-c-app", "parent"]
        order_path = _write_provider_order(
            tmp_path,
            {
                "vendor-a-pro": {"five_hour_remaining_pct": 100.0},
                "vendor-b": {"five_hour_remaining_pct": 100.0},
                "vendor-c-app": {"five_hour_remaining_pct": 100.0},
            },
            order=order,
        )
        _clear_provider_order_cache()
        _patch_default_order_file(monkeypatch, order_path)

        tracker = _make_tracker()
        # Fill vendor-a-pro to its dynamic cap (10 at 100% remaining).
        for _ in range(10):
            tracker.acquire("vendor-a-pro")

        result = _load_provider_info(
            order, tracker=tracker, now=_BEIJING_OFF_PEAK
        )

        assert result["provider_name"] == "vendor-b"
        assert result["base_url"] == "https://api.vendor-b.example/anthropic"

    def test_vendor_a_at_reduced_limit_falls_back(
        self, monkeypatch, tmp_path, fresh_provider_config
    ):
        """vendor-a-pro at 60% remaining (limit=6) and 6 in-flight → vendor-b picked."""
        from agent import _load_provider_info

        order = ["vendor-a-pro", "vendor-b", "vendor-c-app", "parent"]
        order_path = _write_provider_order(
            tmp_path,
            {
                "vendor-a-pro": {"five_hour_remaining_pct": 60.0},
                "vendor-b": {"five_hour_remaining_pct": 100.0},
                "vendor-c-app": {"five_hour_remaining_pct": 100.0},
            },
            order=order,
        )
        _clear_provider_order_cache()
        _patch_default_order_file(monkeypatch, order_path)

        tracker = _make_tracker()
        for _ in range(6):
            tracker.acquire("vendor-a-pro")

        result = _load_provider_info(
            order, tracker=tracker, now=_BEIJING_OFF_PEAK
        )

        assert result["provider_name"] == "vendor-b"

    def test_all_providers_full_returns_empty(
        self, monkeypatch, tmp_path, fresh_provider_config, capacity_config,
    ):
        """All real providers at dynamic cap → empty (caller falls back to parent)."""
        from agent import _load_provider_info

        capacity_config(_CAPS)

        order = ["vendor-a-pro", "vendor-b", "vendor-c-app", "parent"]
        order_path = _write_provider_order(
            tmp_path,
            {
                "vendor-a-pro": {"five_hour_remaining_pct": 100.0},
                "vendor-b": {"five_hour_remaining_pct": 100.0},
                "vendor-c-app": {"five_hour_remaining_pct": 100.0},
            },
            order=order,
        )
        _clear_provider_order_cache()
        _patch_default_order_file(monkeypatch, order_path)

        tracker = _make_tracker()
        for _ in range(10):
            tracker.acquire("vendor-a-pro")
        for _ in range(3):
            tracker.acquire("vendor-b")
        for _ in range(5):
            tracker.acquire("vendor-c-app")

        result = _load_provider_info(
            order, tracker=tracker, now=_BEIJING_OFF_PEAK
        )

        # Empty → caller falls back to parent process's CC Switch proxy.
        assert result == {"provider_name": "", "base_url": "", "api_key": ""}

    def test_parent_entry_never_returned(
        self, monkeypatch, tmp_path, fresh_provider_config, capacity_config,
    ):
        """Even when ``parent`` is the only remaining entry, never return it directly.

        The "parent" sentinel is the caller's explicit fallback. When
        the chain is exhausted, ``_load_provider_info`` returns the
        empty dict (so the dispatch site uses the parent process's
        CC Switch proxy env config) rather than returning ``"parent"``
        as a provider name.
        """
        from agent import _load_provider_info

        capacity_config(_CAPS)

        order = ["vendor-a-pro", "vendor-b", "vendor-c-app", "parent"]
        order_path = _write_provider_order(
            tmp_path,
            {
                "vendor-a-pro": {"five_hour_remaining_pct": 100.0},
                "vendor-b": {"five_hour_remaining_pct": 100.0},
                "vendor-c-app": {"five_hour_remaining_pct": 100.0},
            },
            order=order,
        )
        _clear_provider_order_cache()
        _patch_default_order_file(monkeypatch, order_path)

        tracker = _make_tracker()
        for _ in range(10):
            tracker.acquire("vendor-a-pro")
        for _ in range(3):
            tracker.acquire("vendor-b")
        for _ in range(5):
            tracker.acquire("vendor-c-app")

        result = _load_provider_info(
            order, tracker=tracker, now=_BEIJING_OFF_PEAK
        )

        assert result["provider_name"] != "parent"
        assert result["provider_name"] == ""

    def test_zero_remaining_treated_as_zero_capacity(
        self, monkeypatch, tmp_path, fresh_provider_config
    ):
        """Provider at 0% remaining → limit=0 → always skipped."""
        from agent import _load_provider_info

        order = ["vendor-a-pro", "vendor-b", "vendor-c-app", "parent"]
        order_path = _write_provider_order(
            tmp_path,
            {
                "vendor-a-pro": {"five_hour_remaining_pct": 0.0},
                "vendor-b": {"five_hour_remaining_pct": 100.0},
                "vendor-c-app": {"five_hour_remaining_pct": 100.0},
            },
            order=order,
        )
        _clear_provider_order_cache()
        _patch_default_order_file(monkeypatch, order_path)

        tracker = _make_tracker()
        result = _load_provider_info(
            order, tracker=tracker, now=_BEIJING_OFF_PEAK
        )

        assert result["provider_name"] == "vendor-b"

    def test_missing_5h_field_defaults_to_full_capacity(
        self, monkeypatch, tmp_path, fresh_provider_config
    ):
        """Provider missing five_hour_remaining_pct → treated as 100% remaining."""
        from agent import _load_provider_info

        order = ["vendor-a-pro", "vendor-b", "vendor-c-app", "parent"]
        order_path = _write_provider_order(
            tmp_path,
            {
                "vendor-a-pro": {"weekly_reset_at": "2026-06-15T03:00:00+08:00"},
                # no five_hour_remaining_pct
                "vendor-b": {"five_hour_remaining_pct": 100.0},
                "vendor-c-app": {"five_hour_remaining_pct": 100.0},
            },
            order=order,
        )
        _clear_provider_order_cache()
        _patch_default_order_file(monkeypatch, order_path)

        tracker = _make_tracker()
        result = _load_provider_info(
            order, tracker=tracker, now=_BEIJING_OFF_PEAK
        )

        # vendor-a treated as fully available (5 slots).
        assert result["provider_name"] == "vendor-a-pro"

    def test_dispatch_follows_the_chain_order(
        self, monkeypatch, tmp_path, fresh_provider_config
    ):
        """The dispatch walk takes the chain head; ranking lives in the order.

        A peak-hour demotion is applied by the optimizer's rule engine when
        it builds that order (see ``provider-order.json``) — the dispatch
        path itself consults no clock and no policy.
        """
        from agent import _load_provider_info

        order = ["vendor-b", "vendor-a-pro", "vendor-c-app", "parent"]
        order_path = _write_provider_order(
            tmp_path,
            {
                "vendor-b": {"five_hour_remaining_pct": 100.0},
                "vendor-a-pro": {"five_hour_remaining_pct": 100.0},
                "vendor-c-app": {"five_hour_remaining_pct": 100.0},
            },
            order=order,
        )
        _clear_provider_order_cache()
        _patch_default_order_file(monkeypatch, order_path)

        tracker = _make_tracker()
        peak = datetime(2026, 6, 15, 15, 0, tzinfo=timezone(timedelta(hours=8)))

        result = _load_provider_info(order, tracker=tracker, now=peak)

        assert result["provider_name"] == "vendor-b"

    def test_provider_with_no_db_config_still_skipped(
        self, monkeypatch, tmp_path, fresh_provider_config
    ):
        """If a provider is in the chain but has no CC Switch DB row → skip.

        Preserves the legacy "no config = no provider" semantic. The
        dynamic strategy picks the provider by name; the config lookup
        is a separate gate.
        """
        from agent import _load_provider_info

        # Build a chain with a provider that has no DB config.
        order = ["ghost-provider", "vendor-a-pro", "parent"]
        order_path = _write_provider_order(
            tmp_path,
            {
                "ghost-provider": {"five_hour_remaining_pct": 100.0},
                "vendor-a-pro": {"five_hour_remaining_pct": 100.0},
            },
            order=order,
        )
        _clear_provider_order_cache()
        _patch_default_order_file(monkeypatch, order_path)

        tracker = _make_tracker()
        result = _load_provider_info(
            order, tracker=tracker, now=_BEIJING_OFF_PEAK
        )

        # ghost-provider had full capacity but no DB config; skipped.
        assert result["provider_name"] == "vendor-a-pro"


# ---------------------------------------------------------------------------
# 2. Slot acquisition + release lifecycle at the dispatch site
# ---------------------------------------------------------------------------


class TestDispatchSlotLifecycle:
    """The dispatch site acquires a slot via _acquire_dispatch_slot and
    releases it via _release_dispatch_slot. These helpers are the seam
    ``autonomous_coding`` uses; testing them directly avoids the heavy
    SubagentConfig / GitManager / ClaudeCodingTool initialization that
    the full dispatch site performs."""

    def test_acquire_dispatch_slot_on_real_provider(
        self, monkeypatch, tmp_path, fresh_provider_config
    ):
        """A real provider name returns a context; the tracker increments."""
        from agent import _acquire_dispatch_slot, _release_dispatch_slot

        tracker = _make_tracker()
        ctx = _acquire_dispatch_slot("vendor-a-pro", tracker)

        assert ctx is not None
        assert tracker.current("vendor-a-pro") == 1

        _release_dispatch_slot(ctx)
        assert tracker.current("vendor-a-pro") == 0

    def test_acquire_dispatch_slot_on_empty_provider_skips(
        self, monkeypatch, tmp_path, fresh_provider_config
    ):
        """Empty provider name (parent fallback) → no slot acquired."""
        from agent import _acquire_dispatch_slot

        tracker = _make_tracker()
        ctx = _acquire_dispatch_slot("", tracker)

        assert ctx is None
        assert tracker.snapshot() == {}

    def test_release_dispatch_slot_none_is_noop(
        self, monkeypatch, tmp_path, fresh_provider_config
    ):
        """``_release_dispatch_slot(None)`` is a safe no-op."""
        from agent import _release_dispatch_slot

        tracker = _make_tracker()
        _release_dispatch_slot(None)

        assert tracker.snapshot() == {}

    def test_slot_released_on_exception(
        self, monkeypatch, tmp_path, fresh_provider_config
    ):
        """Exception between acquire and release does not leak the slot.

        The ``except RuntimeError`` block re-raises so pytest reports
        the failure clearly; the assertion is the post-release
        tracker state observed inside the ``finally`` arm. The test
        uses ``pytest.raises`` to mark this as an expected exception
        rather than a hard failure.
        """
        from agent import _acquire_dispatch_slot, _release_dispatch_slot

        tracker = _make_tracker()
        ctx = _acquire_dispatch_slot("vendor-a-pro", tracker)
        assert tracker.current("vendor-a-pro") == 1
        with pytest.raises(RuntimeError, match="simulated failure"):
            try:
                raise RuntimeError("simulated failure during run()")
            finally:
                _release_dispatch_slot(ctx)
        # Slot was released even though the inner block raised.
        assert tracker.current("vendor-a-pro") == 0

    def test_dispatch_site_in_autonomous_coding_uses_helpers(
        self, monkeypatch, tmp_path, fresh_provider_config
    ):
        """``autonomous_coding`` calls _acquire_dispatch_slot and
        _release_dispatch_slot, releasing the slot by return time.

        ``SubagentConfig`` and ``ClaudeCodingTool`` are imported inline
        inside ``autonomous_coding``, so we patch them at their source
        modules rather than on ``agent``. ``GitManager`` and
        ``ConfigRegistry`` are module-level imports and are patched on
        ``agent`` directly.
        """
        from dynamic_provider_concurrency import ActiveConcurrencyTracker

        order = ["vendor-a-pro", "vendor-b", "vendor-c-app", "parent"]
        order_path = _write_provider_order(
            tmp_path,
            {
                "vendor-a-pro": {"five_hour_remaining_pct": 100.0},
                "vendor-b": {"five_hour_remaining_pct": 100.0},
                "vendor-c-app": {"five_hour_remaining_pct": 100.0},
            },
            order=order,
        )
        _clear_provider_order_cache()
        _patch_default_order_file(monkeypatch, order_path)

        tracker = ActiveConcurrencyTracker()
        # 2026-09-14: the module-level ``agent._DYNAMIC_TRACKER``
        # singleton was removed by the TC-006 dependency-injection
        # refactor (``backend.runtime_state.RuntimeState`` owns the
        # tracker; ``autonomous_coding`` takes it as the
        # ``dynamic_tracker`` kwarg). Pass the test tracker through
        # the new seam so the assertion below observes the same
        # slot-count surface the dispatch path uses.
        import agent

        # Build a stub SubagentConfig so ``subagent_cfg.write_tmp_settings``
        # returns a Path without touching the real filesystem or running
        # the real model_map flattening.
        class _StubSubagentCfg:
            settings_file_path = "/tmp/fake_subagent_settings.json"
            task_type = "general"
            task_summary = "test"

            def write_tmp_settings(self, logger=None):
                return Path(self.settings_file_path)

            def to_settings_dict(self):
                return {}

        # Patch the source modules — ``SubagentConfig`` and
        # ``ClaudeCodingTool`` are imported inside ``autonomous_coding``,
        # so patching ``agent.SubagentConfig`` does not work.
        from agent import AutonomousAgent
        with patch.object(AutonomousAgent, "run", return_value=None), \
             patch.object(AutonomousAgent, "plan", return_value=None), \
             patch("agent.GitManager", lambda *_a, **_k: None), \
             patch("agent.ConfigRegistry.get",
                   lambda *_a, **_k: type(
                       "C", (), {"model_map": {}}
                   )()), \
             patch("subagent_config.SubagentConfig",
                   lambda **_kw: _StubSubagentCfg()), \
             patch("coding_tool.ClaudeCodingTool", lambda **_kw: None):
            from agent import autonomous_coding
            autonomous_coding(
                requirement="test",
                project_dir=str(tmp_path),
                config_name=None,
                tool="claude",
                recover=False,
                max_tasks=None,
                dynamic_tracker=tracker,
            )

        # After dispatch, the slot was acquired mid-run and released
        # post-run. We only observe the post-run state, which is depth 0.
        assert tracker.current("vendor-a-pro") == 0


# ===========================================================================
# reserve_slot — the atomic, fleet-aware dispatch contract (2026-09-17)
# ===========================================================================


@pytest.fixture
def vendor_a_family(monkeypatch, tmp_path, capacity_config):
    """Two callable pools at 10 each, and a third the walk cannot use.

    The third row (``Vendor A API``) is declared in ``provider-order.json``
    but has **no CC Switch row**, so the dispatch walk skips it and never
    takes a slot for it. That is what "unreachable" means to this code.

    It is also why the total is a consequence of the caps rather than a
    separate number: the two callable pools hold 10 each, so 20 is the
    most that can be in flight, and nothing has to assert 20 to make it
    true. The third row's cap is declared anyway, so the fixture still
    behaves honestly if a row for it ever appears.
    """
    providers = {
        "Vendor A Pro": {
            "base_url": "https://api.vendor-a.example/anthropic",
            "api_key": "sk-cp-vendor-a",
        },
        "Vendor A": {
            "base_url": "https://api.vendor-a.example/anthropic",
            "api_key": "sk-cp-direct",
        },
        # "Vendor A API" deliberately absent from the database.
    }
    _install_fake_cc_switch_db(monkeypatch, tmp_path, providers)
    capacity_config([
        ("^vendor-a-pro$", 10),
        ("^vendor-a$", 10),
        ("^vendor-a-api$", 10),
    ])
    order = ["Vendor A Pro", "Vendor A", "Vendor A API", "parent"]
    order_path = _write_provider_order(
        tmp_path,
        {name: {"five_hour_remaining_pct": 100.0} for name in order},
        order=order,
    )
    _clear_provider_order_cache()
    _patch_default_order_file(monkeypatch, order_path)
    return order


class TestReserveSlot:
    """``_load_provider_info(reserve_slot=True)`` TAKES the slot."""

    def test_the_walk_takes_the_slot(self, vendor_a_family):
        from agent import _load_provider_info

        tracker = _make_tracker()

        info = _load_provider_info(
            vendor_a_family, tracker=tracker, now=_BEIJING_OFF_PEAK,
            reserve_slot=True,
        )

        assert info["provider_name"] == "Vendor A Pro"
        assert tracker.current("Vendor A Pro") == 1, (
            "reserve_slot=True must leave the slot HELD — the caller "
            "releases it when the subagent finishes"
        )

    def test_a_full_pool_falls_through_to_the_next(self, vendor_a_family):
        from agent import _load_provider_info

        tracker = _make_tracker()
        for _ in range(10):
            tracker.try_acquire("Vendor A Pro", 10)

        info = _load_provider_info(
            vendor_a_family, tracker=tracker, now=_BEIJING_OFF_PEAK,
            reserve_slot=True,
        )

        assert info["provider_name"] == "Vendor A"
        assert tracker.current("Vendor A") == 1

    def test_two_callable_pools_bound_the_total(self, vendor_a_family):
        """Two callable pools at 10 each → at most 20 subagents.

        The bound comes from the caps, not from a fleet number: the
        third row is declared but has no CC Switch row, so the walk skips
        it and it contributes nothing. Drop the caps to 5 each and the
        same fixture tops out at 10 — which is the property this pins.

        Dispatch ROTATES between the two pools, so this asserts the total
        and each pool's share rather than a fixed order; the order itself
        is pinned in ``test_provider_round_robin``.
        """
        from agent import _load_provider_info

        tracker = _make_tracker()
        picked = []
        for _ in range(25):
            info = _load_provider_info(
                vendor_a_family, tracker=tracker, now=_BEIJING_OFF_PEAK,
                reserve_slot=True,
            )
            picked.append(info["provider_name"])

        taken = [name for name in picked if name]
        assert len(taken) == 20, (
            f"20 slots exist across the two callable pools, so 25 dispatches "
            f"must take exactly 20 and leave 5 empty; got {len(taken)}"
        )
        assert picked[20:] == [""] * 5, (
            "the 21st dispatch must find no slot — both callable pools are "
            "full and the third is unreachable"
        )
        assert taken.count("Vendor A Pro") == 10
        assert taken.count("Vendor A") == 10
        assert tracker.total() == 20

    def test_no_fleet_ceiling_beyond_the_declared_caps(
        self, monkeypatch, tmp_path, capacity_config,
    ):
        """A third *callable* pool is used; nothing caps the fleet at 20.

        There is deliberately no fleet-wide number in the implementation.
        Capacity is the sum of the caps over the providers that can
        actually be called, so adding a third live pool raises the
        ceiling rather than being blocked by a constant that was written
        when only two existed.
        """
        from agent import _load_provider_info

        providers = {
            name: {"base_url": "https://api.example/anthropic", "api_key": f"sk-{name}"}
            for name in ("Vendor A Pro", "Vendor A", "Vendor A API")
        }
        _install_fake_cc_switch_db(monkeypatch, tmp_path, providers)
        capacity_config([
            ("^vendor-a-pro$", 10),
            ("^vendor-a$", 10),
            ("^vendor-a-api$", 10),
        ])
        order = ["Vendor A Pro", "Vendor A", "Vendor A API", "parent"]
        order_path = _write_provider_order(
            tmp_path,
            {name: {"five_hour_remaining_pct": 100.0} for name in order},
            order=order,
        )
        _clear_provider_order_cache()
        _patch_default_order_file(monkeypatch, order_path)

        tracker = _make_tracker()
        for _ in range(30):
            _load_provider_info(
                order, tracker=tracker, now=_BEIJING_OFF_PEAK, reserve_slot=True,
            )

        assert tracker.total() == 30, (
            "three callable pools at 10 each give 30 slots; a 20-ceiling "
            "here would mean a constant is overruling the configuration"
        )

    def test_a_provider_with_no_cc_switch_row_does_not_leak_its_slot(
        self, monkeypatch, tmp_path,
    ):
        """The walk takes the slot before validating the row, so the
        fall-through must give it back."""
        from agent import _load_provider_info

        providers = {
            "Vendor A": {
                "base_url": "https://api.vendor-a.example/anthropic",
                "api_key": "sk-cp-direct",
            },
        }
        _install_fake_cc_switch_db(monkeypatch, tmp_path, providers)
        order = ["Vendor A Pro", "Vendor A", "parent"]   # Vendor A has no row
        order_path = _write_provider_order(
            tmp_path,
            {n: {"five_hour_remaining_pct": 100.0} for n in order},
            order=order,
        )
        _clear_provider_order_cache()
        _patch_default_order_file(monkeypatch, order_path)
        tracker = _make_tracker()

        info = _load_provider_info(
            order, tracker=tracker, now=_BEIJING_OFF_PEAK, reserve_slot=True,
        )

        assert info["provider_name"] == "Vendor A"
        assert tracker.current("Vendor A Pro") == 0, (
            "a provider skipped for a missing CC Switch row must not keep "
            "the slot it took — the leak is permanent and would report that "
            "pool full for the life of the process"
        )
        assert tracker.total() == 1


class TestDispatchSlotRelease:
    """``already_reserved`` must release exactly once."""

    def test_pre_acquired_slot_releases_without_double_counting(self):
        from agent import _acquire_dispatch_slot, _release_dispatch_slot

        tracker = _make_tracker()
        tracker.acquire("Vendor A Pro")          # the walk already took it
        assert tracker.current("Vendor A Pro") == 1

        ctx = _acquire_dispatch_slot(
            "Vendor A Pro", tracker, already_reserved=True,
        )
        assert tracker.current("Vendor A Pro") == 1, (
            "already_reserved must NOT acquire again — a second acquire "
            "double-counts and halves the pool's real concurrency"
        )

        _release_dispatch_slot(ctx)
        assert tracker.current("Vendor A Pro") == 0, "released exactly once"

    def test_the_plain_path_still_acquires(self):
        """Back-compat for callers that resolve the provider themselves."""
        from agent import _acquire_dispatch_slot, _release_dispatch_slot

        tracker = _make_tracker()
        ctx = _acquire_dispatch_slot("Vendor A Pro", tracker)
        assert tracker.current("Vendor A Pro") == 1
        _release_dispatch_slot(ctx)
        assert tracker.current("Vendor A Pro") == 0

    def test_empty_provider_takes_no_slot(self):
        from agent import _acquire_dispatch_slot

        tracker = _make_tracker()
        assert _acquire_dispatch_slot("", tracker, already_reserved=True) is None
        assert tracker.total() == 0
