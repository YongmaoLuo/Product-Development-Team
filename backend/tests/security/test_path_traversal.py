"""Security tests for provider-order.json path traversal protection.

VP-024: PROVIDER_ORDER_FILE containing ``../`` must be rejected (or
normalized to a safe path), and absolute paths outside the allowed
directories must also be rejected.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from provider_order import SCHEMA_VERSION, load_fallback_order


SAMPLE_ORDER = ["Vendor A Pro", "Vendor B Pro API", "Vendor C App"]


def _valid_payload(order: list[str] | None = None) -> dict:
    return {
        "version": SCHEMA_VERSION,
        "updated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "source": "test",
        "order": order or SAMPLE_ORDER,
        "providers": {},
    }


def _install_fake_cc_switch_db(home_dir: Path, provider_names: list[str]) -> None:
    """Create a fake ``~/.cc-switch/cc-switch.db`` declaring *provider_names*.

    Uses the production ``providers`` schema, which keys rows by the name
    CC Switch shows — the same string ``provider-order.json`` carries.
    """
    cc_dir = home_dir / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path = cc_dir / "cc-switch.db"
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


def test_rejects_path_with_dotdot_component():
    """Paths with '..' are rejected before any file access."""
    with pytest.raises(ValueError, match="\\.\\."):
        load_fallback_order("/tmp/../etc/passwd")


def test_rejects_absolute_path_outside_allowed_roots():
    """Absolute paths to system files like /etc/passwd are rejected."""
    with pytest.raises(ValueError, match="outside allowed"):
        load_fallback_order("/etc/passwd")


def test_allows_normal_absolute_path_in_tmp(tmp_path, monkeypatch):
    """A plain absolute path under /tmp works normally."""
    _install_fake_cc_switch_db(tmp_path, SAMPLE_ORDER)
    monkeypatch.setenv("HOME", str(tmp_path))

    target = Path("/tmp/test.json")
    target.write_text(json.dumps(_valid_payload()), encoding="utf-8")
    try:
        result = load_fallback_order(target)
        # The path contract is what this file is about: a plain absolute
        # path under /tmp must be accepted and honour the order it
        # declares. Every entry names a live CC Switch row, so the whole
        # list survives the membership filter in its original order.
        assert result == SAMPLE_ORDER
    finally:
        target.unlink(missing_ok=True)
