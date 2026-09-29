"""VP-021 — Unknown kebab-case provider ID must not crash the backend."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pytest

import cc_switch as pcc
from cc_switch import (
    get_provider,
    list_provider_names,
)
from provider_order import (
    REASON_PROVIDER_CONFIG_MISSING,
    SCHEMA_VERSION,
    cache_clear,
    get_provider_order_from_optimizer,
    load_fallback_order,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _make_fake_cc_switch_db(db_path: Path, provider_names: List[str]) -> Path:
    """Create a fake CC Switch DB declaring *provider_names*.

    Production ``providers`` schema: rows carry the name CC Switch shows,
    which is the string the whole backend resolves providers by. The
    ``id`` column is only an opaque row key.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS providers ("
            "id TEXT PRIMARY KEY, name TEXT, settings_config TEXT"
            ")"
        )
        for i, name in enumerate(provider_names):
            settings = {
                "env": {
                    "ANTHROPIC_BASE_URL": f"https://example.com/p{i}/v1",
                    "ANTHROPIC_MODEL": f"fake-model-{i}",
                }
            }
            conn.execute(
                "INSERT OR REPLACE INTO providers (id, name, settings_config) "
                "VALUES (?, ?, ?)",
                (f"row-{i}", name, json.dumps(settings)),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _install_fake_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    known_ids: List[str],
) -> Path:
    cc_dir = tmp_path / ".cc-switch"
    db_path = _make_fake_cc_switch_db(cc_dir / "cc-switch.db", known_ids)
    monkeypatch.setenv("HOME", str(tmp_path))
    return db_path


def _write_order_payload(path: Path, order: List[str]) -> None:
    path.write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "updated_at": _now_iso(),
                "source": "producer",
                "order": order,
                "providers": {},
            }
        ),
        encoding="utf-8",
    )


# Scenario 1 — consumer layer returns None for unknown IDs


def test_consumer_returns_none_for_unknown_kebab_id(tmp_path):
    db_path = _make_fake_cc_switch_db(tmp_path / "cc-switch.db", ["Vendor B Pro"])
    cfg = get_provider("unknown-future-provider", db_path=db_path)
    assert cfg is None


def test_consumer_returns_none_for_multiple_unknown_ids(tmp_path):
    db_path = _make_fake_cc_switch_db(tmp_path / "cc-switch.db", ["Vendor B Pro"])
    for pid in (
        "newvendor-titan",
        "acme-corp-pro",
        "experimental-foundation-llm",
    ):
        assert get_provider(pid, db_path=db_path) is None


def test_consumer_does_not_crash_on_mixed_ids(tmp_path):
    db_path = _make_fake_cc_switch_db(
        tmp_path / "cc-switch.db", ["Vendor B Pro", "Vendor A Pro"]
    )
    assert get_provider("Vendor B Pro", db_path=db_path) is not None
    for pid in ("vendor-c-app", "vendor-c-v2", "vendor-x-not-real"):
        assert get_provider(pid, db_path=db_path) is None


# Scenario 2 — order layer drops unknown IDs with warning


def test_load_fallback_order_drops_unknown_ids(tmp_path, caplog, monkeypatch):
    cache_clear()
    caplog.set_level(logging.WARNING, logger="provider_order")
    _install_fake_home(tmp_path, monkeypatch, ["Vendor B Pro", "Vendor A Pro"])

    target = tmp_path / "provider-order.json"
    _write_order_payload(
        target,
        ["Vendor B Pro", "futurecorp-neo", "Vendor A Pro", "acme-llm-pro"],
    )

    result = load_fallback_order(target)
    # Post-5561e2c: chain carries display names.
    assert result == ["Vendor B Pro", "Vendor A Pro"]


def test_load_fallback_order_logs_warning_for_dropped_ids(
    tmp_path, caplog, monkeypatch
):
    cache_clear()
    caplog.set_level(logging.WARNING, logger="provider_order")
    _install_fake_home(tmp_path, monkeypatch, ["Vendor B Pro"])

    target = tmp_path / "provider-order.json"
    _write_order_payload(
        target,
        ["Vendor B Pro", "unknown-vendor-1", "unknown-vendor-2"],
    )

    result = load_fallback_order(target)
    assert result == ["Vendor B Pro"]

    warnings = [
        rec for rec in caplog.records
        if rec.name == "provider_order" and rec.levelno == logging.WARNING
    ]
    assert any(
        getattr(rec, "reason", None) == REASON_PROVIDER_CONFIG_MISSING
        for rec in warnings
    ), (
        f"expected a provider-config-missing warning; "
        f"reasons={[getattr(rec, 'reason', None) for rec in warnings]!r}"
    )

    matched = False
    for rec in warnings:
        msg = rec.getMessage()
        extras = getattr(rec, "__dict__", {}) or {}
        if "unknown-vendor-1" in msg or "unknown-vendor-1" in str(
            extras.get("dropped", "")
        ):
            matched = True
            break
    assert matched, (
        f"dropped IDs must be identifiable in the warning; "
        f"messages={[rec.getMessage() for rec in warnings]!r}"
    )


def test_load_fallback_order_only_unknown_ids_returns_empty(
    tmp_path, caplog, monkeypatch
):
    cache_clear()
    caplog.set_level(logging.WARNING, logger="provider_order")
    _install_fake_home(tmp_path, monkeypatch, ["Vendor B Pro"])

    target = tmp_path / "provider-order.json"
    _write_order_payload(target, ["alpha-unknown", "beta-unknown", "gamma-unknown"])

    result = load_fallback_order(target)
    assert result == []


# Scenario 3 — optimizer API path filters unknown IDs


def test_get_provider_order_from_optimizer_filters_unknown_ids(tmp_path):
    db_path = _make_fake_cc_switch_db(
        tmp_path / "cc-switch.db", ["Vendor B Pro", "Vendor A Pro"]
    )
    optimizer_output = {
        "order": [
            "Vendor B Pro",
            "unknown-vendor-x",
            "Vendor A Pro",
            "acme-llm",
        ]
    }
    result = get_provider_order_from_optimizer(optimizer_output, db_path=db_path)
    assert result == ["Vendor B Pro", "Vendor A Pro"]


def test_get_provider_order_from_optimizer_all_unknown(tmp_path):
    db_path = _make_fake_cc_switch_db(tmp_path / "cc-switch.db", ["Vendor B Pro"])
    optimizer_output = {"order": ["new-vendor-a", "new-vendor-b", "new-vendor-c"]}
    result = get_provider_order_from_optimizer(optimizer_output, db_path=db_path)
    assert result == []


def test_get_provider_order_from_optimizer_preserves_known_order(tmp_path):
    db_path = _make_fake_cc_switch_db(
        tmp_path / "cc-switch.db", ["alpha-real", "beta-real", "gamma-real"]
    )
    optimizer_output = {
        "order": [
            "alpha-real",
            "ghost-1",
            "beta-real",
            "ghost-2",
            "gamma-real",
        ]
    }
    result = get_provider_order_from_optimizer(optimizer_output, db_path=db_path)
    assert result == ["alpha-real", "beta-real", "gamma-real"]


# Scenario 4 — server.load_providers skips unknown IDs at startup


def test_load_providers_skips_unknown_ids(tmp_path, monkeypatch, caplog):
    cache_clear()
    # Post-5561e2c the chain carries display names, so the fixture uses
    # the production ``providers`` schema and the by-name lookup resolves
    # each surviving entry to a config whose ``id`` is the display name.
    _install_fake_home(tmp_path, monkeypatch, ["Vendor B Pro", "Vendor A Pro"])

    target = tmp_path / "provider-order.json"
    _write_order_payload(
        target,
        ["Vendor B Pro", "ghost-unknown-1", "Vendor A Pro", "ghost-unknown-2"],
    )

    import server

    caplog.set_level(logging.WARNING)
    # Patch the imported reference inside the ``server`` namespace.
    # ``server.load_providers`` calls ``load_fallback_order`` via the
    # name bound at import time, not via ``provider_order.<name>``, so
    # patching ``provider_order.load_fallback_order`` has no effect.
    monkeypatch.setattr(
        server,
        "load_fallback_order",
        lambda *a, **kw: load_fallback_order(target),
    )

    providers = server.load_providers()
    provider_names = [cfg.name for cfg in providers]
    assert "Vendor B Pro" in provider_names
    assert "Vendor A Pro" in provider_names
    assert "ghost-unknown-1" not in provider_names
    assert "ghost-unknown-2" not in provider_names

    warning_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert (
        "ghost-unknown-1" in warning_text
        or "unknown provider" in warning_text.lower()
    ), f"startup must log that unknown IDs were skipped; got: {warning_text!r}"


# Scenario 5 — kebab validation does not reject well-formed unknown IDs




@pytest.mark.parametrize(
    "unknown_id",
    ["unknown-vendor", "ghost-provider", "future-llm"],
)
def test_list_provider_ids_excludes_unknown_ids(tmp_path, unknown_id):
    db_path = _make_fake_cc_switch_db(tmp_path / "cc-switch.db", ["Vendor B Pro"])
    ids = list_provider_names(db_path=db_path)
    assert unknown_id not in ids
    assert ids == ["Vendor B Pro"]


# Scenario 7 — realistic contract file with several unknown IDs


def test_load_fallback_order_realistic_mixed_no_crash(tmp_path, monkeypatch):
    cache_clear()
    _install_fake_home(tmp_path, monkeypatch, ["Vendor B Pro", "Vendor A Pro"])

    target = tmp_path / "provider-order.json"
    _write_order_payload(
        target,
        [
            "Vendor B Pro",
            "vendor-c-app",
            "Vendor A Pro",
            "vendor-c-v2",
            "tencent-hunyuan",
            "vendor-d-chat",
        ],
    )

    result = load_fallback_order(target)
    # Post-5561e2c: chain carries display names.
    assert result == ["Vendor B Pro", "Vendor A Pro"]
