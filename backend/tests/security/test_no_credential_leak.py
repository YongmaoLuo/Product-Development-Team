"""Security tests for backend provider-order reader — credential leak prevention.

VP-023: 日志无 credential 泄露 (异常处理不 dump 整个 JSON 响应).

These tests verify that when ``provider-order.json`` is malformed or
truncated and happens to contain provider secrets in its ``providers``
metadata block, the backend's WARNING logs never emit the secret verbatim
and never dump the full ``providers`` dictionary.

After the consumer-layer migration, malformed or non-dict contract files
raise :class:`provider_order.ProviderOrderError` instead of returning a
fallback chain. The tests here assert the exception is raised and the
warning remains secret-free.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from provider_order import (
    REASON_JSON_INVALID,
    REASON_NOT_A_DICT,
    ProviderOrderError,
    cache_clear,
    load_fallback_order,
)

FAKE_API_KEY = "sk-secret-12345"


def _write_raw(path: Path, raw: str) -> None:
    """Write a raw string to ``path`` — used for malformed-JSON cases."""
    path.write_text(raw, encoding="utf-8")


def _warning_text(caplog: pytest.LogCaptureFixture) -> str:
    """Join every WARNING log message emitted by the ``provider_order`` logger."""
    return " ".join(
        str(rec.message)
        for rec in caplog.records
        if rec.name == "provider_order" and rec.levelno == logging.WARNING
    )


# ---------------------------------------------------------------------------
# Backend read path
# ---------------------------------------------------------------------------


def test_malformed_provider_order_json_does_not_leak_secret(tmp_path, caplog):
    """Malformed provider-order.json containing a fake API key must not leak it.

    The file contains a realistic-looking ``providers`` block with an
    ``api_key`` field, but the JSON is truncated mid-object so
    ``json.load`` raises. The backend must raise :class:`ProviderOrderError`
    and emit a single WARNING that does not contain the secret or the
    full providers dictionary.
    """
    caplog.set_level(logging.WARNING, logger="provider_order")
    target = tmp_path / "provider-order.json"

    # Build a valid-ish payload then truncate it so json.load fails.
    payload = {
        "version": 1,
        "updated_at": "2026-06-15T00:00:00+08:00",
        "source": "producer",
        "order": ["vendor-a-pro"],
        "providers": {
            "vendor-a-pro": {
                "weekly_reset_at": "2026-06-15T03:00:00+08:00",
                "api_key": FAKE_API_KEY,
            }
        },
    }
    full = json.dumps(payload)
    _write_raw(target, full[: len(full) // 2])

    cache_clear()
    with pytest.raises(ProviderOrderError):
        load_fallback_order(target)

    logs = _warning_text(caplog)
    assert FAKE_API_KEY not in logs, (
        "fake API key leaked in backend provider-order warning log"
    )
    # Confirm the failure mode is logged so operators can diagnose it.
    assert REASON_JSON_INVALID in logs
    assert "could not be parsed" in logs


def test_not_a_dict_provider_order_does_not_leak_secret(tmp_path, caplog):
    """A non-dict root with an embedded secret must not leak it either."""
    caplog.set_level(logging.WARNING, logger="provider_order")
    target = tmp_path / "provider-order.json"

    # A JSON array that embeds the secret — root is not a dict.
    _write_raw(target, f'["{FAKE_API_KEY}", "vendor-a-pro"]')

    cache_clear()
    with pytest.raises(ProviderOrderError):
        load_fallback_order(target)

    logs = _warning_text(caplog)
    assert FAKE_API_KEY not in logs
    assert REASON_NOT_A_DICT in logs
