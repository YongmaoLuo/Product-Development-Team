"""VP-017 verification: agent.py has no hardcoded provider IDs.

Self-contained tests covering:
  * agent.py has no inlined provider ID constants in dispatch logic
    (provider order / config comes from the consumer layer).
  * ``select_provider`` is a thin walk over the chain it is handed — the
    order carries every ranking policy (the optimizer's rule engine owns
    peak-hour demotions), so no clock or policy is consulted here.
"""

import re
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import agent
from agent import _load_provider_info


AGENT_PATH = Path(agent.__file__)


def _strip_docstrings_and_comments(src):
    """Remove triple-quoted blocks and comment lines from source.

    Lets the static scan focus on executable code only — agent.py
    intentionally keeps provider IDs in docstrings/schema examples
    to document the consumer-layer contract.
    """
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    src = re.sub(r"^\s*#.*$", "", src, flags=re.MULTILINE)
    return src


def test_no_hardcoded_bare_provider_ids_in_executable_code():
    """Executable agent.py code must not contain bare legacy IDs.

    Allowed:
      * ``_VENDOR_B_CANONICAL = "vendor-b-pro"`` (the canonical alias)
      * ``should_degrade_vendor-b("vendor-b-pro", ...)`` (the shared-rule contract arg)
    Everything else must come from the consumer layer at runtime.
    """
    raw = AGENT_PATH.read_text()
    src = _strip_docstrings_and_comments(raw)

    forbidden = []
    for m in re.finditer(r'"(vendor-b|vendor-a(?:-vendor-a)?|vendor-b-pro)"', src):
        literal = m.group(1)
        # Window of ±80 chars around the match.
        window = src[max(0, m.start() - 80): m.end() + 80]
        if "_VENDOR_B_CANONICAL" in window and "=" in window:
            continue
        if "should_degrade_vendor-b(" in window:
            continue
        forbidden.append((src[:m.start()].count("\n") + 1, literal))

    assert not forbidden, (
        "agent.py contains hardcoded provider IDs in executable code: "
        + repr(forbidden)
    )


def test_provider_order_loaded_dynamically():
    """agent.py must source provider order from provider_order.load_fallback_order."""
    src = AGENT_PATH.read_text()
    assert "load_fallback_order" in src, (
        "agent.py must call load_fallback_order to obtain the provider chain "
        "dynamically (no inlined provider list)."
    )


def test_provider_config_consumed_from_cc_switch_db():
    """Provider connection params come from the CC Switch module."""
    src = AGENT_PATH.read_text()
    assert "from cc_switch import" in src and "get_provider(" in src, (
        "agent.py must resolve base_url/api_key through "
        "cc_switch.get_provider — no hardcoded provider dicts."
    )


PEAK = datetime(2026, 6, 15, 15, 0, 0)
RESET_FAR = PEAK + timedelta(days=3)
RESET_SOON = PEAK + timedelta(hours=12)


def test_select_provider_returns_the_chain_head():
    """``select_provider`` is a thin walk: the ORDER is the policy.

    The chain (``provider-order.json``) already carries the optimizer's
    ranking — including any rule-driven peak-hour demotion — so this
    function consults no clock and no policy of its own.
    """
    assert agent.select_provider(["vendor-b", "vendor-a-pro"], now=PEAK) == "vendor-b"


def test_select_provider_returns_none_for_an_empty_chain():
    assert agent.select_provider([], now=PEAK) is None


def test_load_provider_info_reads_consumer_layer(monkeypatch, tmp_path):
    """_load_provider_info resolves base_url/api_key from the CC Switch DB
    via cc_switch — no hardcoded provider dict in agent.py."""
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
    conn.execute(
        "INSERT INTO provider_configs (id, url, model, extra_params) "
        "VALUES (?, ?, ?, ?)",
        (
            "vendor-a-pro",
            "https://api.vendor-a.example/anthropic",
            "fake-model",
            json.dumps({"ANTHROPIC_AUTH_TOKEN": "sk-fake-vendor-a-key-1234567890"}),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HOME", str(fake_home))

    order_path = tmp_path / "provider-order.json"
    order_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": datetime.now(timezone.utc).astimezone().isoformat(),
                "source": "producer",
                "order": ["vendor-a-pro", "parent"],
                "providers": {
                    "vendor-a-pro": {"five_hour_remaining_pct": 100.0}
                },
            }
        )
    )

    import provider_order
    monkeypatch.setattr(provider_order, "_default_order_file", lambda: order_path)
    provider_order.cache_clear()

    from dynamic_provider_concurrency import ActiveConcurrencyTracker
    off_peak = datetime(2026, 6, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))

    result = _load_provider_info(
        ["vendor-a-pro", "parent"],
        tracker=ActiveConcurrencyTracker(),
        now=off_peak,
    )
    assert result["provider_name"] == "vendor-a-pro"
    assert result["base_url"] == "https://api.vendor-a.example/anthropic"
    assert result["api_key"] == "sk-fake-vendor-a-key-1234567890"
