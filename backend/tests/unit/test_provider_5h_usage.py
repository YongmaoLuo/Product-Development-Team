"""
TDD tests for ``backend/provider_order.py::load_provider_5h_usage``.

The dynamic-concurrency module reads each provider's 5-hour remaining
quota percentage from ``provider-order.json``. The new reader is a
sibling of :func:`provider_order.load_fallback_order`:

  * Same on-disk contract (``provider-order.json`` schema v1+).
  * Same caching discipline (once-per-process, keyed on resolved path).
  * New optional per-provider field: ``five_hour_remaining_pct`` in
    ``providers.<id>``.

Failure modes — all default to "100% remaining" (treat the provider as
fully available, so the dispatch loop falls back to the legacy
fixed-cap behaviour):

  * File missing             → ``{}`` (empty map, defaults kick in)
  * Malformed JSON           → ``{}``
  * Missing ``providers``    → ``{}``
  * Provider missing entry   → not in the returned map (defaults)
  * ``five_hour_remaining_pct`` missing
                             → not in the returned map (defaults)
  * Value not a number       → not in the returned map (defaults)
  * Value outside [0, 100]   → clamped into [0, 100]

Rounding semantics
------------------
The on-disk value is a percentage (0-100, possibly float). No rounding
applied here — the consumer (:func:`compute_dynamic_limit`) rounds
half-up internally. The reader passes values through verbatim so a
diagnostic "remaining 73.4%" is not silently truncated to 73.

Staleness
---------
``provider-order.json`` is refreshed by the optimizer subprocess on a
poll interval (default 30 min). The reader does NOT enforce freshness
itself — the wiring layer (agent.py) decides whether to re-fetch
live when the file is too old. Pinning the staleness policy in two
places (this reader and the agent) would split the contract.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict

import pytest

from provider_order import (
    REASON_FILE_MISSING,
    cache_clear,
    load_provider_5h_usage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_payload(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat()


# ---------------------------------------------------------------------------
# 1. Happy path: file with valid five_hour_remaining_pct per provider
# ---------------------------------------------------------------------------


def test_happy_path_returns_5h_pct_per_provider(tmp_path):
    """Valid file → map of provider_id → five_hour_remaining_pct."""
    cache_clear()
    target = tmp_path / "provider-order.json"
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["vendor-a-pro", "vendor-b", "vendor-c-app"],
        "providers": {
            "vendor-a-pro": {
                "weekly_reset_at": _now_iso(),
                "five_hour_remaining_pct": 75.5,
            },
            "vendor-b": {
                "weekly_reset_at": _now_iso(),
                "five_hour_remaining_pct": 40.0,
            },
            "vendor-c-app": {
                "weekly_reset_at": _now_iso(),
                "five_hour_remaining_pct": 100.0,
            },
        },
    }
    _write_payload(target, payload)

    result = load_provider_5h_usage(target)

    assert result == {
        "vendor-a-pro": 75.5,
        "vendor-b": 40.0,
        "vendor-c-app": 100.0,
    }


def test_value_preserved_verbatim(tmp_path):
    """Float percentages are NOT truncated to int by the reader."""
    cache_clear()
    target = tmp_path / "provider-order.json"
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["vendor-a-pro"],
        "providers": {
            "vendor-a-pro": {"five_hour_remaining_pct": 73.456},
        },
    }
    _write_payload(target, payload)

    result = load_provider_5h_usage(target)

    assert result["vendor-a-pro"] == 73.456


# ---------------------------------------------------------------------------
# 2. Missing five_hour_remaining_pct → provider absent from result
# ---------------------------------------------------------------------------


def test_provider_without_5h_field_absent_from_result(tmp_path):
    """Provider entry exists but lacks five_hour_remaining_pct → omitted."""
    cache_clear()
    target = tmp_path / "provider-order.json"
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["vendor-a-pro", "vendor-b"],
        "providers": {
            "vendor-a-pro": {"weekly_reset_at": _now_iso()},  # no 5h field
            "vendor-b": {"five_hour_remaining_pct": 80.0},
        },
    }
    _write_payload(target, payload)

    result = load_provider_5h_usage(target)

    assert "vendor-a-pro" not in result
    assert result["vendor-b"] == 80.0


def test_value_out_of_range_clamped(tmp_path):
    """Values < 0 clamp to 0, > 100 clamp to 100."""
    cache_clear()
    target = tmp_path / "provider-order.json"
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["vendor-a-pro", "vendor-b"],
        "providers": {
            "vendor-a-pro": {"five_hour_remaining_pct": -10.0},
            "vendor-b": {"five_hour_remaining_pct": 250.0},
        },
    }
    _write_payload(target, payload)

    result = load_provider_5h_usage(target)

    assert result["vendor-a-pro"] == 0.0
    assert result["vendor-b"] == 100.0


def test_non_numeric_value_skipped(tmp_path):
    """``five_hour_remaining_pct`` is a string → provider omitted."""
    cache_clear()
    target = tmp_path / "provider-order.json"
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["vendor-a-pro", "vendor-b"],
        "providers": {
            "vendor-a-pro": {"five_hour_remaining_pct": "lots"},
            "vendor-b": {"five_hour_remaining_pct": 50.0},
        },
    }
    _write_payload(target, payload)

    result = load_provider_5h_usage(target)

    assert "vendor-a-pro" not in result
    assert result["vendor-b"] == 50.0


# ---------------------------------------------------------------------------
# 3. Failure modes → empty map (defaults to 100% in caller)
# ---------------------------------------------------------------------------


def test_missing_file_returns_empty_map(tmp_path):
    """File does not exist → {} (caller treats all providers as 100%)."""
    cache_clear()
    missing = tmp_path / "does-not-exist.json"

    result = load_provider_5h_usage(missing)

    assert result == {}


def test_malformed_json_returns_empty_map(tmp_path):
    """Garbage JSON → {} (do not crash the dispatch loop)."""
    cache_clear()
    target = tmp_path / "provider-order.json"
    target.write_text("{not valid json", encoding="utf-8")

    result = load_provider_5h_usage(target)

    assert result == {}


def test_providers_missing_returns_empty_map(tmp_path):
    """Valid JSON but no ``providers`` key → {}."""
    cache_clear()
    target = tmp_path / "provider-order.json"
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["vendor-a-pro", "vendor-b"],
        # no "providers" key
    }
    _write_payload(target, payload)

    result = load_provider_5h_usage(target)

    assert result == {}


def test_providers_not_a_dict_returns_empty_map(tmp_path):
    """``providers`` is a list, not a dict → {} (silent ignore)."""
    cache_clear()
    target = tmp_path / "provider-order.json"
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["vendor-a-pro", "vendor-b"],
        "providers": ["vendor-a-pro", "vendor-b"],
    }
    _write_payload(target, payload)

    result = load_provider_5h_usage(target)

    assert result == {}


# ---------------------------------------------------------------------------
# 4. Caching — once-per-process, cleared by cache_clear()
# ---------------------------------------------------------------------------


def test_caching_returns_same_dict_within_process(tmp_path):
    """Two consecutive calls return the same dict (no extra IO)."""
    cache_clear()
    target = tmp_path / "provider-order.json"
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["vendor-a-pro"],
        "providers": {"vendor-a-pro": {"five_hour_remaining_pct": 60.0}},
    }
    _write_payload(target, payload)

    first = load_provider_5h_usage(target)
    second = load_provider_5h_usage(target)

    # Same content; the cache returns a defensive copy each call so
    # callers can mutate their local without poisoning the cache.
    assert first == second == {"vendor-a-pro": 60.0}


def test_cache_clear_forces_refetch(tmp_path):
    """``cache_clear()`` makes the next call re-read the file."""
    cache_clear()
    target = tmp_path / "provider-order.json"
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["vendor-a-pro"],
        "providers": {"vendor-a-pro": {"five_hour_remaining_pct": 60.0}},
    }
    _write_payload(target, payload)
    first = load_provider_5h_usage(target)
    assert first == {"vendor-a-pro": 60.0}

    # Mutate the on-disk file.
    payload["providers"]["vendor-a-pro"]["five_hour_remaining_pct"] = 30.0
    _write_payload(target, payload)

    # Without cache_clear: stale value.
    stale = load_provider_5h_usage(target)
    assert stale == {"vendor-a-pro": 60.0}

    # After cache_clear: fresh value.
    cache_clear()
    fresh = load_provider_5h_usage(target)
    assert fresh == {"vendor-a-pro": 30.0}


# ---------------------------------------------------------------------------
# 5. Default-file resolution — None → loader falls back to on-disk default
# ---------------------------------------------------------------------------


def test_none_path_uses_default_file(monkeypatch, tmp_path):
    """``file_path=None`` resolves to the standard on-disk default.

    We monkeypatch the default resolver to return ``tmp_path/...`` so
    the test does not touch the real on-disk contract file. The shape
    is the same as the production code path.
    """
    cache_clear()
    target = tmp_path / "provider-order.json"
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["vendor-a-pro"],
        "providers": {"vendor-a-pro": {"five_hour_remaining_pct": 42.0}},
    }
    _write_payload(target, payload)

    # Patch the default-file resolver to point at our tmp file.
    monkeypatch.setattr(
        "provider_order._default_order_file",
        lambda: target,
    )

    result = load_provider_5h_usage()

    assert result == {"vendor-a-pro": 42.0}
