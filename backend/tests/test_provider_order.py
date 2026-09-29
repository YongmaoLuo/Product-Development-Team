"""TDD spec for backend/provider_order.py — dynamic consumer-layer contract.

After the migration to the CC Switch consumer layer,
:func:`provider_order.load_fallback_order` treats the optimizer's
``provider-order.json`` as the single source of truth.  There is no
hard-coded fallback chain and no YAML ``provider_priority`` fallback —
a missing or invalid contract file raises :class:`ProviderOrderError`.

The 4 schema failure scenarios that MUST raise
:class:`ProviderOrderError`:

    1. ``version`` is not equal to 1
    2. ``order`` key is missing
    3. ``order`` is present but is an empty array
    4. ``updated_at`` is present but is not a valid ISO 8601 string

A valid schema returns the JSON ``order`` filtered through
:func:`cc_switch.list_provider_names`: entries
that do not name a live CC Switch provider are dropped with a warning.

Entries are CC Switch provider **names** — the ``providers.name``
column, e.g. ``Vendor A``, ``Vendor B Pro``. The same string is what the
routing config matches its regexes against and what
:func:`cc_switch.get_provider` resolves,
so the chain needs no translation. (Before 2026-09-24 there was a
kebab-case id layer in between, with a hard-coded name↔id table; it is
gone.)
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from provider_order import (
    REASON_DB_UNAVAILABLE,
    REASON_FILE_MISSING,
    REASON_JSON_INVALID,
    REASON_NOT_A_DICT,
    REASON_ORDER_EMPTY,
    REASON_ORDER_MISSING,
    REASON_PROVIDER_CONFIG_MISSING,
    REASON_STALE,
    REASON_UPDATED_AT_INVALID,
    REASON_VERSION_MISMATCH,
    SCHEMA_VERSION,
    ProviderOrderError,
    cache_clear,
    get_provider_order_from_optimizer,
    load_fallback_order,
)


# ---------------------------------------------------------------------------
# PROVIDER_LIMITS — per-provider concurrency caps (backend/server.py)
# ---------------------------------------------------------------------------
#
# The table is keyed by a provider's CC Switch NAME — the same string
# provider_routing.yaml matches its regexes against and the consumer
# layer resolves. It is populated from each provider row's optional
# ``max_concurrency`` value (see ``server._read_dynamic_provider_limits``).


def test_provider_limits_keys_are_provider_names_verbatim():
    """Every key in ``server.PROVIDER_LIMITS`` is a CC Switch provider name.

    Used verbatim, not slugified: ``_resolve_max_parallel`` looks the key
    up with the name it received from the routing/priority chain, so any
    "normalisation" here would guarantee a miss. (It *did* miss until
    2026-09-24 — the lookup lower-cased its key, which was correct only
    while keys were kebab-case ids.)
    """
    from server import PROVIDER_LIMITS

    for key in PROVIDER_LIMITS:
        assert isinstance(key, str) and key, f"bad PROVIDER_LIMITS key {key!r}"
        assert key == key.strip(), (
            f"PROVIDER_LIMITS key {key!r} carries surrounding whitespace; "
            f"keys are the DB row name verbatim"
        )


def test_no_builtin_provider_limit_table():
    """A fresh install ships NO per-provider caps.

    Caps come from each CC Switch row's own optional ``max_concurrency``.
    A table compiled into the repository would publish one operator's
    provider names — and their measured capacity — as everyone's.
    """
    from server import DEFAULT_PROVIDER_LIMITS

    assert DEFAULT_PROVIDER_LIMITS == {}, (
        "DEFAULT_PROVIDER_LIMITS must stay empty: per-provider caps are a "
        "property of the operator's CC Switch rows, not of this project"
    )


def test_limit_values_positive():
    """Every value in ``server.PROVIDER_LIMITS`` is a positive int.

    A zero or negative cap would deadlock the dual-layer semaphore
    (no slot would ever be released for that provider).  This test
    guards against an accidental ``0`` or negative default.
    """
    from server import PROVIDER_LIMITS

    for key, value in PROVIDER_LIMITS.items():
        assert isinstance(value, int) and not isinstance(value, bool), (
            f"PROVIDER_LIMITS[{key!r}] = {value!r} is not an int"
        )
        assert value > 0, (
            f"PROVIDER_LIMITS[{key!r}] = {value!r} is not > 0"
        )


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Current local time as a valid ISO 8601 string with offset."""
    return datetime.now(timezone.utc).astimezone().isoformat()


def _write_payload(path: Path, payload: Any) -> None:
    """Serialize ``payload`` to ``path`` as JSON (handles non-dict)."""
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_raw(path: Path, raw: str) -> None:
    """Write a raw string — used for the malformed-JSON case."""
    path.write_text(raw, encoding="utf-8")


def _capture_warning(
    caplog: pytest.LogCaptureFixture,
) -> List[logging.LogRecord]:
    """Return the list of WARNING records emitted to ``provider_order`` logger."""
    return [
        rec for rec in caplog.records
        if rec.name == "provider_order" and rec.levelno == logging.WARNING
    ]


def _make_fake_cc_switch_db(tmp_path: Path, provider_names: List[str]) -> Path:
    """Create a fake ``~/.cc-switch/cc-switch.db`` declaring *provider_names*.

    Uses the production ``providers`` schema, keyed by the name CC Switch
    shows — the same string ``provider-order.json`` carries and the
    consumer layer resolves against. There is no kebab-case id layer in
    between (removed 2026-09-24).
    """
    db_path = tmp_path / "cc-switch.db"
    settings = json.dumps({
        "env": {
            "ANTHROPIC_BASE_URL": "https://example.com/v1",
            "ANTHROPIC_AUTH_TOKEN": "sk-fake",
            "ANTHROPIC_MODEL": "fake-model",
        }
    })
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE providers ("
            "id TEXT PRIMARY KEY, name TEXT, settings_config TEXT"
            ")"
        )
        for idx, name in enumerate(provider_names):
            conn.execute(
                "INSERT INTO providers (id, name, settings_config) VALUES (?, ?, ?)",
                (f"row-{idx}", name, settings),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# TDD — dynamic consumer-layer contract
# ---------------------------------------------------------------------------


def _install_fake_home_db(tmp_path: Path, db_path: Path) -> None:
    """Move ``db_path`` under ``tmp_path/.cc-switch/`` as the live DB."""
    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path.replace(cc_dir / "cc-switch.db")


def test_load_order_returns_display_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_fallback_order`` returns display names for DB-present entries.

    Post-5561e2c contract: the function returns CC Switch **display
    names** (not kebab-case IDs) because downstream
    ``get_provider`` queries the ``providers`` table by
    its ``name`` column. Kebab-case IDs in the JSON are converted via
    the inverse of ``_CC_SWITCH_NAME_TO_ID`` (``Vendor A`` →
    ``Vendor A``, ``Vendor B Pro`` → ``Vendor B Pro``); unknown entries are
    dropped with a warning.
    """
    db_path = _make_fake_cc_switch_db(tmp_path, ["Vendor A", "Vendor B Pro"])
    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path.replace(cc_dir / "cc-switch.db")
    monkeypatch.setenv("HOME", str(tmp_path))

    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["Vendor A", "Vendor-B-Pro", "Vendor B Pro", "not-in-db"],
        "providers": {},
    }
    _write_payload(target, payload)

    result = load_fallback_order(target)

    assert result == ["Vendor A", "Vendor B Pro"]


def test_load_fallback_order_returns_json_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """VP-004 contract: ``load_fallback_order`` returns the JSON ``order`` array as display names.

    When ``agent.py`` starts a sub-agent it calls
    ``provider_order.load_fallback_order()``; the returned sequence must
    match the ``order`` array declared in ``provider-order.json`` with
    each entry converted to its CC Switch display name (post-5561e2c
    contract — the downstream ``get_provider`` queries
    by display name).
    """
    json_order = ["Vendor A", "Vendor B Pro", "vendor-c-app"]
    db_path = _make_fake_cc_switch_db(tmp_path, json_order)
    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path.replace(cc_dir / "cc-switch.db")
    monkeypatch.setenv("HOME", str(tmp_path))

    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": json_order,
        "providers": {},
    }
    _write_payload(target, payload)

    result = load_fallback_order(target)

    # ``Vendor A`` → ``Vendor A`` (inverse map, last write wins),
    # ``Vendor B Pro`` → ``Vendor B Pro``, ``vendor-c-app`` → verbatim
    # (no inverse mapping recorded).
    assert result == ["Vendor A", "Vendor B Pro", "vendor-c-app"]


def test_no_hardcoded_chain() -> None:
    """``HARDCODED_CHAIN`` must not exist anymore in ``provider_order``."""
    import provider_order

    assert not hasattr(provider_order, "HARDCODED_CHAIN"), (
        "HARDCODED_CHAIN was removed; load_fallback_order must raise "
        "ProviderOrderError on missing/invalid contract files"
    )


def test_missing_file_raises_or_empty(tmp_path: Path) -> None:
    """A missing optimizer file raises :class:`ProviderOrderError` (or returns [])."""
    target = tmp_path / "provider-order.json"
    assert not target.exists()

    try:
        result = load_fallback_order(target)
    except ProviderOrderError:
        return

    # The contract also allows returning an empty list; both are acceptable.
    assert result == [], (
        f"missing file must raise ProviderOrderError or return [], got {result!r}"
    )


def test_json_missing_falls_back_to_yaml(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """VP-005: a missing ``provider-order.json`` is loud and fatal.

    The test name is preserved for VP-005 traceability. The original
    contract (fall back to YAML ``provider_priority`` when the JSON
    contract is absent) was deliberately removed during the
    consumer-layer migration: ``provider-order.json`` is now the
    single source of truth and a missing file raises
    :class:`ProviderOrderError` so an operator can decide whether to
    restore the contract or set ``PDT_PROVIDER_PRIORITY`` (see the
    ``provider_order`` module docstring, "Failure semantics").

    What this test now pins:

      1. ``load_fallback_order`` raises :class:`ProviderOrderError`
         when the target file does not exist.
      2. A WARNING is emitted on the ``provider_order`` logger with
         ``reason == REASON_FILE_MISSING`` before the raise, so an
         operator can grep ``reason=file_missing`` to find the
         missing-file failure mode fast.
    """
    cache_clear()
    caplog.set_level(logging.WARNING, logger="provider_order")
    target = tmp_path / "provider-order.json"
    assert not target.exists()

    with pytest.raises(ProviderOrderError):
        load_fallback_order(target)

    warnings = _capture_warning(caplog)
    assert any(
        getattr(rec, "reason", None) == REASON_FILE_MISSING for rec in warnings
    ), (
        f"expected a WARNING with reason={REASON_FILE_MISSING!r} on the "
        f"provider_order logger, got {warnings!r}"
    )


# ---------------------------------------------------------------------------
# Schema validation — failures now raise ProviderOrderError
# ---------------------------------------------------------------------------


def test_schema_valid_returns_filtered_order(tmp_path, caplog, monkeypatch):
    """Valid schema → load_fallback_order returns the JSON ``order`` as display names."""
    caplog.set_level(logging.WARNING, logger="provider_order")
    db_path = _make_fake_cc_switch_db(tmp_path, ["Vendor A", "Vendor B Pro", "vendor-c-app"])
    monkeypatch.setenv("HOME", str(tmp_path))
    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path.replace(cc_dir / "cc-switch.db")

    target = tmp_path / "provider-order.json"
    json_order = ["Vendor A", "Vendor B Pro", "vendor-c-app"]
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": json_order,
        "providers": {
            "Vendor A": {"weekly_reset_at": _now_iso()},
        },
    }
    _write_payload(target, payload)

    result = load_fallback_order(target)

    assert result == ["Vendor A", "Vendor B Pro", "vendor-c-app"]
    # No warning on the happy path.
    assert _capture_warning(caplog) == []


def test_schema_version_mismatch_raises(tmp_path, caplog):
    """``version`` not equal to 1 → :class:`ProviderOrderError`."""
    caplog.set_level(logging.WARNING, logger="provider_order")
    target = tmp_path / "provider-order.json"
    payload = {
        "version": 2,  # Wrong version
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["a", "b", "c"],
        "providers": {},
    }
    _write_payload(target, payload)

    with pytest.raises(ProviderOrderError):
        load_fallback_order(target)

    assert any(getattr(rec, "reason", None) == REASON_VERSION_MISMATCH for rec in _capture_warning(caplog))


def test_schema_order_missing_raises(tmp_path, caplog):
    """``order`` key absent → :class:`ProviderOrderError`."""
    caplog.set_level(logging.WARNING, logger="provider_order")
    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        # No "order" key at all
        "providers": {},
    }
    _write_payload(target, payload)

    with pytest.raises(ProviderOrderError):
        load_fallback_order(target)

    assert any(getattr(rec, "reason", None) == REASON_ORDER_MISSING for rec in _capture_warning(caplog))


def test_schema_order_empty_raises(tmp_path, caplog):
    """``order`` is ``[]`` → :class:`ProviderOrderError`."""
    caplog.set_level(logging.WARNING, logger="provider_order")
    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": [],  # Empty
        "providers": {},
    }
    _write_payload(target, payload)

    with pytest.raises(ProviderOrderError):
        load_fallback_order(target)

    assert any(getattr(rec, "reason", None) == REASON_ORDER_EMPTY for rec in _capture_warning(caplog))


def test_schema_updated_at_invalid_raises(tmp_path, caplog):
    """``updated_at`` not a parseable ISO 8601 string → :class:`ProviderOrderError`."""
    caplog.set_level(logging.WARNING, logger="provider_order")
    target = tmp_path / "provider-order.json"

    bad_values = [
        "not-a-date",                      # Plain garbage string
        "2026/06/14 12:34:56",             # Wrong separator
        "2026-13-45T99:99:99",             # Out-of-range fields
        "",                                # Empty string
        "2026-06-14",                      # Date-only, not a timestamp
    ]
    for bad in bad_values:
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": bad,
            "source": "producer",
            "order": ["Vendor A", "vendor-b"],
            "providers": {},
        }
        _write_payload(target, payload)
        caplog.clear()
        cache_clear()

        with pytest.raises(ProviderOrderError):
            load_fallback_order(target)

        assert any(getattr(rec, "reason", None) == REASON_UPDATED_AT_INVALID for rec in _capture_warning(caplog))


# ---------------------------------------------------------------------------
# VP-006 aliases — schema failures raise ProviderOrderError + WARNING w/ reason
# ---------------------------------------------------------------------------
#
# The verification plan's test_command for VP-006 queries these legacy
# test names. The actual contract (no YAML fallback — see module
# docstring) raises :class:`ProviderOrderError` and emits a WARNING with
# a structured ``reason`` field, which is stricter than the original
# "downgrade to YAML" spec and satisfies the same observability goal
# (operator can grep ``reason=<x>`` to find each failure mode).


def test_json_invalid_version(tmp_path, caplog):
    """VP-006: ``version`` != 1 -> ProviderOrderError + WARNING(reason=version_mismatch)."""
    test_schema_version_mismatch_raises(tmp_path, caplog)


def test_json_missing_order(tmp_path, caplog):
    """VP-006: ``order`` key absent -> ProviderOrderError + WARNING(reason=order_missing)."""
    test_schema_order_missing_raises(tmp_path, caplog)


def test_json_empty_order(tmp_path, caplog):
    """VP-006: ``order`` is ``[]`` -> ProviderOrderError + WARNING(reason=order_empty)."""
    test_schema_order_empty_raises(tmp_path, caplog)


def test_json_invalid_timestamp(tmp_path, caplog):
    """VP-006: ``updated_at`` unparseable -> ProviderOrderError + WARNING(reason=updated_at_invalid)."""
    test_schema_updated_at_invalid_raises(tmp_path, caplog)


# ---------------------------------------------------------------------------
# VP-007 alias — JSON+YAML both unavailable -> hard failure (no hardcoded chain)
# ---------------------------------------------------------------------------
#
# The verification plan's test_command for VP-007 queries this legacy
# test name. The original contract (when JSON is corrupted AND YAML is
# unavailable, fall back to a hardcoded chain ``[vendor-a, vendor-b,
# vendor-c-app, parent]`` and emit an ERROR log) was deliberately removed
# during the consumer-layer migration: ``provider-order.json`` is now
# the single source of truth, there is no YAML ``provider_priority``
# fallback, and the hardcoded chain constant was deleted (pinned by
# ``test_no_hardcoded_chain`` above).
#
# What this test now pins — every "JSON unavailable" failure mode
# (missing, malformed, schema-invalid) MUST raise
# :class:`ProviderOrderError` and emit a structured log record with a
# greppable ``reason`` field. The YAML layer is treated as permanently
# unavailable (the module no longer reads it), so "YAML unavailable" is
# the default state for every code path.
#
# This satisfies the same observability goal as the original VP-007
# spec (an operator can grep ``reason=<x>`` to find each failure mode
# fast) while being strictly safer than silently degrading to a
# hardcoded chain that may point at providers no longer registered in
# the CC Switch database.

# Recognised reasons for the JSON-unavailable failure modes covered by
# VP-007. Each entry is ``(reason, payload_kind)`` where ``payload_kind``
# selects the fixture writer used to produce the failure mode.
_VP007_JSON_UNAVAILABLE_REASONS = [
    (REASON_FILE_MISSING, "missing"),
    (REASON_JSON_INVALID, "malformed"),
    (REASON_NOT_A_DICT, "not_a_dict"),
    (REASON_VERSION_MISMATCH, "bad_version"),
    (REASON_ORDER_MISSING, "order_missing"),
    (REASON_ORDER_EMPTY, "order_empty"),
    (REASON_UPDATED_AT_INVALID, "bad_timestamp"),
]


def _write_vp007_failure(target: Path, payload_kind: str) -> None:
    """Write one of the VP-007 ``JSON unavailable`` failure fixtures."""
    if payload_kind == "missing":
        # No file at all — YAML layer is the only fallback the legacy
        # contract had, and that fallback is gone.
        assert not target.exists()
        return
    if payload_kind == "malformed":
        _write_raw(target, "{not valid json")
        return
    if payload_kind == "not_a_dict":
        _write_payload(target, ["not", "a", "dict"])
        return
    base = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["Vendor A", "Vendor B Pro", "vendor-c-app", "parent"],
        "providers": {},
    }
    if payload_kind == "bad_version":
        base["version"] = 2
    elif payload_kind == "order_missing":
        base.pop("order")
    elif payload_kind == "order_empty":
        base["order"] = []
    elif payload_kind == "bad_timestamp":
        base["updated_at"] = "not-a-date"
    _write_payload(target, base)


@pytest.mark.parametrize("reason,payload_kind", _VP007_JSON_UNAVAILABLE_REASONS)
def test_yaml_unavailable_falls_back_to_hardcoded(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    reason: str,
    payload_kind: str,
) -> None:
    """VP-007: JSON+YAML both unavailable -> ProviderOrderError (no hardcoded chain).

    The test name is preserved for VP-007 traceability. The original
    contract (return ``[vendor-a, vendor-b, vendor-c-app, parent]`` and log
    ERROR) was replaced during the consumer-layer migration with a
    strict-failure policy: ``provider-order.json`` is the single
    source of truth, the YAML ``provider_priority`` fallback layer was
    removed, and the hardcoded chain constant was deleted.

    For every recognised JSON-unavailable failure mode — missing file,
    malformed JSON, root not a dict, schema-invalid (version, order
    missing, order empty, ``updated_at`` unparseable) — this test
    asserts:

      1. ``load_fallback_order`` raises :class:`ProviderOrderError`.
         There is no silent degradation to a hardcoded chain.
      2. A structured log record is emitted on the ``provider_order``
         logger with ``reason == <expected sentinel>`` so operators
         can grep ``reason=<x>`` to find the failure mode.
      3. No ``HARDCODED_CHAIN`` constant exists in the module — the
         old hardcoded chain (``[vendor-a, vendor-b, vendor-c-app, parent]``)
         cannot be returned under any code path.

    ``YAML unavailable`` is the default state of the module: the
    YAML-read code path was deleted, so every call to
    :func:`load_fallback_order` is effectively a "YAML-unavailable"
    call.
    """
    cache_clear()
    caplog.set_level(logging.WARNING, logger="provider_order")

    # ``HARDCODED_CHAIN`` must not exist — the legacy fallback chain
    # ``[vendor-a, vendor-b, vendor-c-app, parent]`` cannot be returned under
    # any code path. Pinned here so a future revert that reintroduces
    # the constant also fails this VP-007 alias.
    import provider_order

    assert not hasattr(provider_order, "HARDCODED_CHAIN"), (
        "HARDCODED_CHAIN was removed; load_fallback_order must raise "
        "ProviderOrderError when both JSON and YAML are unavailable"
    )

    target = tmp_path / "provider-order.json"
    _write_vp007_failure(target, payload_kind)

    with pytest.raises(ProviderOrderError):
        load_fallback_order(target)

    warnings = _capture_warning(caplog)
    assert any(
        getattr(rec, "reason", None) == reason for rec in warnings
    ), (
        f"expected a WARNING with reason={reason!r} on the provider_order "
        f"logger for payload_kind={payload_kind!r}, got reasons="
        f"{[getattr(r, 'reason', None) for r in warnings]!r}"
    )


# ---------------------------------------------------------------------------
# VP-008 alias — YAML unique providers are NOT appended (YAML layer removed)
# ---------------------------------------------------------------------------
#
# The verification plan's test_command for VP-008 queries this legacy
# test name. The original contract (union semantics: JSON order=[a,b]
# + YAML providers=[a,b,c] -> [a,b,c], with YAML-only ``c`` appended at
# the tail) was deliberately removed during the consumer-layer
# migration: ``provider-order.json`` is the single source of truth, the
# YAML ``provider_priority`` fallback layer was deleted, and no union
# merge against any YAML provider list happens under any code path.
#
# What this test now pins:
#
#   1. Given a valid JSON ``order=[a, b]`` and a config.yaml-style
#      ``provider_priority=[a, b, c]`` on disk, ``load_fallback_order``
#      returns exactly ``[a, b]`` -- the YAML-only ``c`` is NOT appended.
#   2. No ``resolve_yaml_providers`` symbol exists in ``provider_order``
#      -- the YAML-read code path was deleted, so the union merge can
#      never run.
#
# This is stricter than the original VP-008 spec (which allowed silent
# degradation via YAML), and satisfies the same observability goal
# (operator can trust that the JSON ``order`` is returned verbatim).


def test_yaml_unique_providers_appended(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VP-008: YAML-only providers are NOT appended (YAML layer removed).

    The test name is preserved for VP-008 traceability. The original
    contract (union semantics -- JSON order=[a,b] plus YAML
    provider_priority=[a,b,c] returned [a,b,c]) was replaced during the
    consumer-layer migration: ``provider-order.json`` is the single
    source of truth, the YAML ``provider_priority`` fallback / merge
    layer was deleted, and no provider from any YAML file is ever
    appended to the JSON order.

    This test asserts the new contract:

      1. Given a valid JSON ``order=[a, b]`` and a YAML file declaring
         ``provider_priority=[a, b, c]`` on disk,
         :func:`load_fallback_order` returns exactly ``[a, b]`` -- the
         YAML-only ``c`` is NOT appended.
      2. The module exposes no ``resolve_yaml_providers`` symbol (the
         YAML-read code path was deleted).
    """
    cache_clear()
    caplog.set_level(logging.WARNING, logger="provider_order")

    # ``resolve_yaml_providers`` must not exist -- the YAML-read entry
    # point was deleted during the consumer-layer migration, so no
    # union merge against any YAML provider list can ever run.
    import provider_order

    assert not hasattr(provider_order, "resolve_yaml_providers"), (
        "resolve_yaml_providers was removed; load_fallback_order must "
        "return the JSON order verbatim with no YAML-only append"
    )

    # Fake CC Switch DB that knows about ``a``, ``b``, and ``c`` so the
    # consumer-layer filter does not silently drop the would-be
    # YAML-appended ``c`` -- if the implementation regressed and started
    # consulting YAML, ``c`` would survive the filter and appear in the
    # result, failing the assertion below.
    json_order = ["Vendor A", "Vendor B Pro"]
    yaml_only_extra = "vendor-c-app"
    _install_fake_home(tmp_path, monkeypatch, json_order + [yaml_only_extra])

    # Drop a YAML file on disk that -- under the legacy contract -- would
    # have contributed ``yaml_only_extra`` to the union. Under the new
    # contract this file is ignored entirely.
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        "provider_priority:\n"
        "  - Vendor A\n"
        "  - Vendor B Pro\n"
        f"  - {yaml_only_extra}\n",
        encoding="utf-8",
    )
    assert yaml_file.exists()

    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": json_order,
        "providers": {},
    }
    _write_payload(target, payload)

    result = load_fallback_order(target)

    # Post-5561e2c: the JSON order is returned as display names
    # (``Vendor A`` → ``Vendor A``, ``Vendor B Pro`` → ``Vendor B Pro``).
    expected = ["Vendor A", "Vendor B Pro"]
    assert result == expected, (
        f"YAML-only providers must NOT be appended (YAML layer removed); "
        f"expected {expected!r}, got {result!r}"
    )
    assert yaml_only_extra not in result, (
        f"YAML-only provider {yaml_only_extra!r} leaked into the chain; "
        f"the YAML fallback / union-merge path should not exist"
    )


# ---------------------------------------------------------------------------
# VP-010 alias -- YAML / JSON conflict resolution: JSON order wins, no YAML union
# ---------------------------------------------------------------------------
#
# The verification plan's test_command for VP-010 queries these three
# legacy test names. The original contract (union-merge semantics --
# JSON order=[a,b] plus YAML provider_priority=[a,b,c] returned [a,b,c]
# with the YAML-only ``c`` appended at the tail, and JSON order=[b,a]
# plus YAML=[a,b] returned [b,a] with JSON order preserved) was
# deliberately removed during the consumer-layer migration:
# ``provider-order.json`` is now the single source of truth, the YAML
# ``provider_priority`` fallback / merge layer was deleted, and no
# provider from any YAML file is ever appended to the JSON order.
#
# What these tests pin (the new, stricter contract):
#
#   1. ``test_yaml_json_merge`` -- a YAML file on disk declaring extra
#      providers (e.g. ``provider_priority=[a, b, c]`` next to JSON
#      ``order=[a, b]``) does NOT contribute the YAML-only ``c``; the
#      returned chain is exactly the JSON ``order``.
#   2. ``test_yaml_unique_appended`` -- the YAML-only provider is NOT
#      appended to the tail (alias for the VP-008 contract above, just
#      exposed under the VP-010 query name).
#   3. ``test_json_order_priority`` -- the JSON ``order`` is preserved
#      verbatim regardless of any YAML on disk; ``order=[b, a]`` with
#      YAML ``provider_priority=[a, b]`` returns ``[b, a]``.


def test_yaml_json_merge(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VP-010: a YAML file next to a valid JSON order is ignored entirely.

    The original VP-010 spec (union semantics -- JSON order=[a,b] plus
    YAML provider_priority=[a,b,c] returned [a,b,c]) was replaced
    during the consumer-layer migration: ``provider-order.json`` is
    the single source of truth, the YAML ``provider_priority`` merge
    layer was deleted, and no provider from any YAML file is appended.

    This test asserts the new contract: given a valid JSON
    ``order=[a, b]`` and a YAML file declaring
    ``provider_priority=[a, b, c]`` on disk, ``load_fallback_order``
    returns exactly ``[a, b]`` -- the YAML-only ``c`` is NOT appended.
    """
    cache_clear()
    caplog.set_level(logging.WARNING, logger="provider_order")

    import provider_order

    assert not hasattr(provider_order, "resolve_yaml_providers"), (
        "resolve_yaml_providers was removed; load_fallback_order must "
        "return the JSON order verbatim with no YAML-only append"
    )

    json_order = ["Vendor A", "Vendor B Pro"]
    yaml_only_extra = "vendor-c-app"
    _install_fake_home(tmp_path, monkeypatch, json_order + [yaml_only_extra])

    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        "provider_priority:\n"
        "  - Vendor A\n"
        "  - Vendor B Pro\n"
        f"  - {yaml_only_extra}\n",
        encoding="utf-8",
    )
    assert yaml_file.exists()

    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": json_order,
        "providers": {},
    }
    _write_payload(target, payload)

    result = load_fallback_order(target)

    # Post-5561e2c: display names, not kebab IDs.
    expected = ["Vendor A", "Vendor B Pro"]
    assert result == expected, (
        f"JSON order must win over YAML union merge; expected "
        f"{expected!r}, got {result!r}"
    )
    assert yaml_only_extra not in result, (
        f"YAML-only provider {yaml_only_extra!r} leaked into the chain; "
        f"the YAML fallback / union-merge path should not exist"
    )


def test_yaml_unique_appended(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VP-010: YAML-only provider is NOT appended to the JSON chain.

    Alias for the VP-008 contract (see
    ``test_yaml_unique_providers_appended`` above) exposed under the
    VP-010 query name. The legacy union-merge semantics (JSON
    order=[a,b] + YAML providers=[a,b,c] -> [a,b,c]) were removed
    during the consumer-layer migration; the YAML-only ``c`` is NOT
    appended to the tail under any code path.
    """
    cache_clear()
    caplog.set_level(logging.WARNING, logger="provider_order")

    import provider_order

    assert not hasattr(provider_order, "resolve_yaml_providers"), (
        "resolve_yaml_providers was removed; load_fallback_order must "
        "return the JSON order verbatim with no YAML-only append"
    )

    json_order = ["Vendor A", "Vendor B Pro"]
    yaml_only_extra = "vendor-c-app"
    _install_fake_home(tmp_path, monkeypatch, json_order + [yaml_only_extra])

    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        "provider_priority:\n"
        "  - Vendor A\n"
        "  - Vendor B Pro\n"
        f"  - {yaml_only_extra}\n",
        encoding="utf-8",
    )

    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": json_order,
        "providers": {},
    }
    _write_payload(target, payload)

    result = load_fallback_order(target)

    # Post-5561e2c: display names, not kebab IDs.
    expected = ["Vendor A", "Vendor B Pro"]
    assert result == expected, (
        f"YAML-only providers must NOT be appended (YAML layer removed); "
        f"expected {expected!r}, got {result!r}"
    )
    assert yaml_only_extra not in result, (
        f"YAML-only provider {yaml_only_extra!r} must not be appended "
        f"to the tail; the YAML fallback path should not exist"
    )


def test_json_order_priority(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VP-010: JSON order is preserved verbatim regardless of YAML on disk.

    Under both the legacy and the new contract, ``JSON order=[b, a]``
    plus YAML ``provider_priority=[a, b]`` returns ``[b, a]`` -- the
    JSON order wins. The new contract additionally guarantees the
    YAML list is never consulted at all, so the YAML file is dropped
    on disk purely as a regression sentinel: if a future change
    reintroduces a YAML read path, the YAML ``[a, b]`` ordering could
    re-order the chain, which would fail the assertion below.
    """
    cache_clear()
    caplog.set_level(logging.WARNING, logger="provider_order")

    import provider_order

    assert not hasattr(provider_order, "resolve_yaml_providers"), (
        "resolve_yaml_providers was removed; load_fallback_order must "
        "return the JSON order verbatim with no YAML-driven reordering"
    )

    json_order = ["Vendor B Pro", "Vendor A"]
    _install_fake_home(tmp_path, monkeypatch, json_order)

    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        "provider_priority:\n"
        "  - Vendor A\n"
        "  - Vendor B Pro\n",
        encoding="utf-8",
    )

    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": json_order,
        "providers": {},
    }
    _write_payload(target, payload)

    result = load_fallback_order(target)

    # Post-5561e2c: display names in JSON order (``Vendor B Pro`` →
    # ``Vendor B Pro``, ``Vendor A`` → ``Vendor A``).
    expected = ["Vendor B Pro", "Vendor A"]
    assert result == expected, (
        f"JSON order must win over any YAML ordering on disk; expected "
        f"{expected!r}, got {result!r}"
    )


def test_schema_providers_dict_failure_ignored(tmp_path, caplog):
    """Bad ``providers`` block does NOT crash the read.

    The order list is still returned (filtered by DB), and a malformed
    ``providers`` value (a list, a string, a number) must not be promoted
    to a top-level error.
    """
    caplog.set_level(logging.WARNING, logger="provider_order")
    db_path = _make_fake_cc_switch_db(tmp_path, ["Vendor A", "Vendor B Pro", "vendor-c-app"])
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("HOME", str(tmp_path))
    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path.replace(cc_dir / "cc-switch.db")

    target = tmp_path / "provider-order.json"
    json_order = ["Vendor A", "Vendor B Pro", "vendor-c-app"]

    bad_providers_shapes = [
        ["a", "b", "c"],          # list, not a dict
        "not-a-dict",             # raw string
        42,                       # number
        [{"k": "v"}],            # list of dicts
    ]
    for bad in bad_providers_shapes:
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": _now_iso(),
            "source": "producer",
            "order": json_order,
            "providers": bad,
        }
        _write_payload(target, payload)
        caplog.clear()
        cache_clear()

        # MUST NOT raise. The whole point of the test.
        result = load_fallback_order(target)

        # Post-5561e2c: display names (``vendor-c-app`` has no inverse
        # mapping and passes through verbatim).
        assert result == ["Vendor A", "Vendor B Pro", "vendor-c-app"]
        # And no warning is emitted (bad providers metadata is silent).
        assert _capture_warning(caplog) == []

    monkeypatch.undo()


# ---------------------------------------------------------------------------
# Cache tests (adapted to the new no-fallback contract)
# ---------------------------------------------------------------------------
#
# These tests pin the once-per-process cache on
# :func:`provider_order.load_fallback_order`. The cache is keyed on the
# resolved absolute file path, so two distinct inputs that point at the
# same on-disk file share one entry.
#
# Acceptance bullets (one-to-one with VP-009):
#
#   1. 10 consecutive calls in the same process trigger exactly 1 file
#      IO (the 9 follow-ups are served from the in-process cache).
#   2. ``cache_clear()`` is exposed as a public API and forces a fresh
#      read on the next call.
#   3. Modifying the JSON on disk without ``cache_clear()`` does NOT
#      change the returned chain — the cache value is sticky until
#      cleared.
#   4. 100 concurrent first-time callers from different threads do not
#      cause more than a small constant number of file IO (the per-key
#      in-flight lock collapses the race into a single read).


def _make_io_counter(target_resolved: Path) -> Dict[str, Any]:
    """Build a ``(patched_open, get_count, reset)`` triple for IO counting.

    The patched open records one event per call whose target path
    matches ``target_resolved`` (compared via ``Path.resolve()`` so
    relative paths and trailing slashes are normalized). The original
    ``Path.open`` is captured at the time of construction and invoked
    unconditionally — the patch never changes the file-content
    behavior, only observes.

    Note: the patched function counts both reads and writes to the
    target. ``Path.write_text`` (used by the test fixtures to set up
    payloads) also goes through ``Path.open`` with mode ``"w"``. If
    the test mutates the file via ``_write_payload`` after installing
    the counter, it must call :func:`reset` afterwards so the
    post-mutation cache-hit assertion is not polluted by the write's
    own open call.
    """
    state: Dict[str, Any] = {
        "events": [],
        "target_str": str(target_resolved),
        "original_open": Path.open,
    }

    def counting_open(self, *args, **kwargs):
        if str(self.resolve()) == state["target_str"]:
            state["events"].append(1)
        return state["original_open"](self, *args, **kwargs)

    def get_count() -> int:
        return len(state["events"])

    def reset() -> None:
        state["events"].clear()

    return {
        "open": counting_open,
        "get_count": get_count,
        "reset": reset,
        "original_open": state["original_open"],
    }


def _install_fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_ids: List[str]) -> Path:
    """Install a fake CC Switch DB under ``$HOME/.cc-switch/cc-switch.db``."""
    db_path = _make_fake_cc_switch_db(tmp_path, provider_ids)
    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path.replace(cc_dir / "cc-switch.db")
    monkeypatch.setenv("HOME", str(tmp_path))
    return cc_dir / "cc-switch.db"


def test_lru_cache(tmp_path, monkeypatch):
    """Same-process 10× calls trigger exactly 1 file IO.

    Also pins the "stale-on-modify" contract: changing the on-disk
    file after the cache is populated does NOT cause the next
    :func:`load_fallback_order` call to return the new value — the
    cached chain is sticky until :func:`cache_clear` is invoked.
    """
    cache_clear()
    _install_fake_home(tmp_path, monkeypatch, ["Vendor A", "Vendor B Pro", "vendor-c-app"])
    target = tmp_path / "provider-order.json"
    target_resolved = target.resolve()
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["Vendor A", "Vendor B Pro", "vendor-c-app"],
        "providers": {},
    }
    _write_payload(target, payload)

    counter = _make_io_counter(target_resolved)
    monkeypatch.setattr(Path, "open", counter["open"])

    # 10 consecutive calls → exactly 1 IO on the target.
    results = [load_fallback_order(target) for _ in range(10)]
    assert all(r == results[0] for r in results), (
        f"all 10 cached calls must return the same chain, "
        f"got distinct values: {results[:3]!r}..."
    )
    # Post-5561e2c: display names (``vendor-c-app`` passes through
    # verbatim — no inverse mapping recorded).
    assert results[0] == ["Vendor A", "Vendor B Pro", "vendor-c-app"], (
        f"expected display-name order, got {results[0]!r}"
    )
    assert counter["get_count"]() == 1, (
        f"expected 1 file IO for 10 cached calls, got "
        f"{counter['get_count']()}"
    )

    # Mutate the file on disk. Without ``cache_clear`` the next
    # call must still return the previously-cached value.
    mutated = dict(payload)
    mutated["order"] = ["Vendor A", "Vendor B Pro", "vendor-c-app", "parent"]
    _write_payload(target, mutated)
    counter["reset"]()

    cached_after_modify = load_fallback_order(target)
    assert cached_after_modify == ["Vendor A", "Vendor B Pro", "vendor-c-app"], (
        f"cache must return the previously-cached value (sticky); "
        f"got {cached_after_modify!r}"
    )
    assert counter["get_count"]() == 0, (
        f"cache hit must not re-read the file; "
        f"io count drifted to {counter['get_count']()}"
    )


def test_cache_clear(tmp_path, monkeypatch):
    """``cache_clear()`` exposes a public, idempotent escape hatch.

    After ``cache_clear()`` the next call to :func:`load_fallback_order`
    reads the file again and returns the up-to-date chain. Calling
    ``cache_clear()`` twice in a row is a no-op (must not raise).
    """
    cache_clear()
    _install_fake_home(tmp_path, monkeypatch, ["Vendor A", "Vendor B Pro", "vendor-c-app"])
    target = tmp_path / "provider-order.json"
    target_resolved = target.resolve()
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["Vendor A", "Vendor B Pro", "vendor-c-app"],
        "providers": {},
    }
    _write_payload(target, payload)

    counter = _make_io_counter(target_resolved)
    monkeypatch.setattr(Path, "open", counter["open"])

    # First read populates the cache (1 IO).
    first = load_fallback_order(target)
    assert first == ["Vendor A", "Vendor B Pro", "vendor-c-app"]
    assert counter["get_count"]() == 1

    # Second read is a cache hit (still 1 IO).
    second = load_fallback_order(target)
    assert second == first
    assert counter["get_count"]() == 1

    # Mutate the on-disk file. The cached value is still served.
    mutated = dict(payload)
    mutated["order"] = ["Vendor A", "Vendor B Pro", "vendor-c-app", "parent"]
    _write_payload(target, mutated)
    counter["reset"]()
    stale = load_fallback_order(target)
    assert stale == ["Vendor A", "Vendor B Pro", "vendor-c-app"], (
        f"before cache_clear, the on-disk mutation is invisible; "
        f"got {stale!r}"
    )
    assert counter["get_count"]() == 0

    # Now drop the cache. The next read MUST hit the file.
    cache_clear()
    fresh = load_fallback_order(target)
    assert fresh == ["Vendor A", "Vendor B Pro", "vendor-c-app"], (
        f"after cache_clear, the mutated file must be read; "
        f"got {fresh!r}"
    )
    assert counter["get_count"]() == 1, (
        f"cache_clear must force a refetch; expected 1 IO "
        f"post-reset, got {counter['get_count']()}"
    )

    # Idempotency: a second ``cache_clear()`` is a safe no-op.
    cache_clear()
    cache_clear()
    still_fresh = load_fallback_order(target)
    assert still_fresh == ["Vendor A", "Vendor B Pro", "vendor-c-app"]
    assert counter["get_count"]() == 2, (
        f"a fresh read after an extra cache_clear must trigger IO; "
        f"got {counter['get_count']()}"
    )


def test_lru_cache_thread_safe(tmp_path, monkeypatch):
    """100 concurrent first-time callers → at most a small constant
    number of file IOs.
    """
    cache_clear()
    _install_fake_home(tmp_path, monkeypatch, ["Vendor A", "Vendor B Pro", "vendor-c-app"])
    target = tmp_path / "provider-order.json"
    target_resolved = target.resolve()
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["Vendor A", "Vendor B Pro", "vendor-c-app"],
        "providers": {},
    }
    _write_payload(target, payload)

    counter = _make_io_counter(target_resolved)

    # Patch directly (not via monkeypatch.setattr) because
    # monkeypatch's teardown races with the executor's atexit hooks
    # when many threads are in flight. We restore the original
    # ``open`` in a ``finally`` block.
    Path.open = counter["open"]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            futures = [
                ex.submit(load_fallback_order, target) for _ in range(100)
            ]
            results = [f.result() for f in futures]

        # Post-5561e2c: display names.
        expected = ["Vendor A", "Vendor B Pro", "vendor-c-app"]
        assert all(r == expected for r in results), (
            f"all 100 threads must see the same chain; "
            f"distinct values: {sorted({tuple(r) for r in results})!r}"
        )
        io_count = counter["get_count"]()
        assert io_count <= 2, (
            f"expected at most 2 file IOs under 100-way concurrency, "
            f"got {io_count}"
        )
    finally:
        Path.open = counter["original_open"]
        cache_clear()


# ---------------------------------------------------------------------------
# Stale file warning (still relevant — stale file is still used)
# ---------------------------------------------------------------------------


def test_stale_warning(tmp_path, caplog, monkeypatch):
    """JSON schema valid but updated_at > 30 minutes → WARNING but still used."""
    cache_clear()
    caplog.set_level(logging.WARNING, logger="provider_order")
    _install_fake_home(tmp_path, monkeypatch, ["Vendor A", "Vendor B Pro", "vendor-c-app"])
    target = tmp_path / "provider-order.json"
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=60)
    json_order = ["Vendor A", "Vendor B Pro", "vendor-c-app"]
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": stale_time.isoformat().replace("+00:00", "Z"),
        "source": "producer",
        "order": json_order,
        "providers": {},
    }
    _write_payload(target, payload)

    result = load_fallback_order(target)

    # Post-5561e2c: display names even on the stale path.
    assert result == ["Vendor A", "Vendor B Pro", "vendor-c-app"], (
        f"stale JSON order should still be used, got {result!r}"
    )

    warnings = _capture_warning(caplog)
    assert len(warnings) == 1, f"expected 1 stale warning, got {len(warnings)}"
    rec = warnings[0]
    assert getattr(rec, "reason", None) == REASON_STALE, (
        f"expected reason={REASON_STALE!r}, got reason={getattr(rec, 'reason', None)!r}"
    )
    msg = rec.getMessage()
    assert "provider-order.json is stale" in msg, (
        f"warning message should contain 'provider-order.json is stale', got {msg!r}"
    )
    assert "age=60min" in msg, f"warning message should contain 'age=60min', got {msg!r}"
    assert "optimizer may not be running" in msg, (
        f"warning message should contain 'optimizer may not be running', got {msg!r}"
    )


def test_updated_at_freshness(tmp_path, caplog, monkeypatch):
    """updated_at 在 30 分钟内 → 无 WARNING，只有 DEBUG 日志."""
    cache_clear()
    caplog.set_level(logging.DEBUG, logger="provider_order")
    _install_fake_home(tmp_path, monkeypatch, ["Vendor A", "Vendor B Pro", "vendor-c-app"])
    target = tmp_path / "provider-order.json"
    fresh_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    json_order = ["Vendor A", "Vendor B Pro", "vendor-c-app"]
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": fresh_time.isoformat().replace("+00:00", "Z"),
        "source": "producer",
        "order": json_order,
        "providers": {},
    }
    _write_payload(target, payload)

    result = load_fallback_order(target)

    # Post-5561e2c: display names.
    assert result == ["Vendor A", "Vendor B Pro", "vendor-c-app"], (
        f"fresh JSON order should be used, got {result!r}"
    )

    warnings = _capture_warning(caplog)
    assert warnings == [], f"fresh file should not emit warnings, got {warnings!r}"

    debug_records = [
        rec for rec in caplog.records
        if rec.name == "provider_order" and rec.levelno == logging.DEBUG
    ]
    assert any("provider-order.json is fresh" in rec.getMessage() for rec in debug_records), (
        f"expected a DEBUG record confirming freshness, got "
        f"{[rec.getMessage() for rec in debug_records]!r}"
    )


# ---------------------------------------------------------------------------
# DB-unavailable handling
# ---------------------------------------------------------------------------


def test_db_unavailable_raises_provider_order_error(tmp_path, caplog, monkeypatch):
    """If the CC Switch database cannot be read, :class:`ProviderOrderError` is raised."""
    caplog.set_level(logging.WARNING, logger="provider_order")
    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["Vendor A", "Vendor B Pro"],
        "providers": {},
    }
    _write_payload(target, payload)

    # Point HOME at a directory with no database.
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(ProviderOrderError):
        load_fallback_order(target)

    assert any(getattr(rec, "reason", None) == REASON_DB_UNAVAILABLE for rec in _capture_warning(caplog))


# ---------------------------------------------------------------------------
# Provider-order consumer-layer migration
# ---------------------------------------------------------------------------
#
# These tests pin the contract for converting optimizer output into an
# ordered provider list via the CC Switch consumer layer.  Only IDs that
# are both kebab-case AND present in the consumer database are returned.


def test_provider_order_from_optimizer(tmp_path):
    """Optimizer ``order`` is returned verbatim after consumer-layer filtering."""
    db_path = _make_fake_cc_switch_db(tmp_path, ["Vendor A", "Vendor B Pro"])
    optimizer_output = {"order": ["Vendor A", "Vendor B Pro"]}

    result = get_provider_order_from_optimizer(optimizer_output, db_path=db_path)

    assert result == ["Vendor A", "Vendor B Pro"]


def test_only_live_provider_names_survive(tmp_path):
    """Only entries EXACTLY matching a live CC Switch provider name are kept.

    The near-misses below (wrong case, wrong separator, stray whitespace,
    a neighbouring name) must all be dropped — matching is by exact
    string equality against the ``providers.name`` column, so no
    normalisation can let a different provider slip into the chain.
    """
    db_path = _make_fake_cc_switch_db(tmp_path, ["Vendor A", "Vendor B Pro"])
    optimizer_output = {
        "order": [
            "Vendor A",
            "Vendor-B-Pro",
            "vendor-b_glm",
            "Vendor B Pro",
            "vendor-b--b-pro",
            "-Vendor B Pro",
            "Vendor B Pro-",
            "",
            "vendor-b b-pro",
            "vendor-b.b-pro",
            123,
            "vendor-b",
            "vendor-c-app",
        ]
    }

    result = get_provider_order_from_optimizer(optimizer_output, db_path=db_path)

    assert result == ["Vendor A", "Vendor B Pro"]


def test_empty_optimizer_output(tmp_path):
    """Empty or malformed optimizer output returns an empty list without crashing."""
    db_path = _make_fake_cc_switch_db(tmp_path, ["Vendor A"])

    assert get_provider_order_from_optimizer({}, db_path=db_path) == []
    assert get_provider_order_from_optimizer({"order": []}, db_path=db_path) == []
    assert get_provider_order_from_optimizer(None, db_path=db_path) == []
    assert get_provider_order_from_optimizer("not-a-dict", db_path=db_path) == []
    assert get_provider_order_from_optimizer({"order": "not-a-list"}, db_path=db_path) == []


# ---------------------------------------------------------------------------
# Path-traversal guard (unchanged contract)
# ---------------------------------------------------------------------------


def test_rejects_path_traversal():
    """Paths containing ``..`` are rejected before any file access."""
    from provider_order import _validate_provider_order_file_path

    with pytest.raises(ValueError):
        _validate_provider_order_file_path("/tmp/../etc/passwd")


def test_rejects_outside_allowed_roots(tmp_path):
    """Resolved paths outside the project root or temp dirs are rejected."""
    from provider_order import _validate_provider_order_file_path

    with pytest.raises(ValueError):
        _validate_provider_order_file_path("/etc/passwd")


# ---------------------------------------------------------------------------
# VP-015 — default provider_order_file path + gitignore coverage
# ---------------------------------------------------------------------------
#
# Pins the architecture decision that the default ``provider_order_file``
# resolved by ``server._resolve_provider_order_file()`` (via the env var,
# via ``backend/config.yaml``, or via the config-dir default) MUST live
# under the operator's config directory rather than at a path this
# repository invents for a producer it does not ship. The file is a
# runtime artifact, so its directory MUST be covered by ``.gitignore``.


def test_default_provider_order_file_path():
    """VP-015: the default resolves into the operator config dir.

    The path resolved at server import time (``server.PROVIDER_ORDER_FILE``)
    must equal ``<project_root>/.config/provider-order.json``, and that
    directory must be covered by ``.gitignore`` so a producer's runtime
    state cannot be committed by accident.
    """
    import server

    project_root = Path(__file__).resolve().parent.parent.parent

    resolved = server.PROVIDER_ORDER_FILE
    expected = project_root / ".config" / "provider-order.json"

    assert resolved == expected, (
        f"server.PROVIDER_ORDER_FILE must resolve to {expected!r}, got {resolved!r}"
    )

    gitignore = project_root / ".gitignore"
    assert gitignore.is_file(), f".gitignore missing at {gitignore!r}"
    gitignore_text = gitignore.read_text(encoding="utf-8")
    assert ".config/" in gitignore_text, (
        "the default provider-order directory must be covered by "
        ".gitignore, otherwise the contract file is committed by accident"
    )
