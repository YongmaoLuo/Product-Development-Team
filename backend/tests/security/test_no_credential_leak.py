"""No credential reaches the log from either provider-order reader.

Two readers still touch data that can carry provider secrets, and
neither is allowed to echo what it read:

* :func:`provider_order.load_provider_5h_usage` — the 5-hour quota
  payload on disk, whose ``providers`` block carries API keys.
* :func:`provider_order._chain_from_api` — CC Switch's HTTP provider
  list, which is served from the same rows.

The property pinned here is narrow and mechanical: a malformed or
hostile payload containing a secret must produce a log line that names
the *failure* without reproducing the *content*. These readers report
what went wrong structurally — a reason, a type, a position — and never
dump the document, because a log line is the one place a secret that
was supposed to stay in a 0600 file reliably ends up readable by
everyone with access to the log.

The two readers' failure semantics differ — the quota reader degrades
to "no quota data", the chain reader falls back to its database — and
each case below asserts the one its reader actually has.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import provider_order
from provider_order import (
    REASON_JSON_INVALID,
    REASON_NOT_A_DICT,
    ProviderOrderError,
    cache_clear,
    load_provider_5h_usage,
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


class _Response:
    """Stand-in for what ``urlopen`` hands back."""

    status = 200

    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(monkeypatch, raw: bytes) -> None:
    """Make CC Switch's HTTP provider list answer with ``raw`` bytes."""
    monkeypatch.setattr(
        provider_order.urllib.request,
        "urlopen",
        lambda *a, **k: _Response(raw),
    )


# ---------------------------------------------------------------------------
# The quota payload on disk
# ---------------------------------------------------------------------------


def test_malformed_provider_order_json_does_not_leak_secret(tmp_path, caplog):
    """A truncated payload with a fake API key must not leak it.

    The file contains a realistic-looking ``providers`` block with an
    ``api_key`` field, but the JSON is truncated mid-object so parsing
    fails. The reader must degrade to "no quota data" and log a
    warning that does not contain the secret or the block it was in.
    """
    caplog.set_level(logging.WARNING, logger="provider_order")
    target = tmp_path / "provider-order.json"

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
    assert load_provider_5h_usage(target) == {}

    logs = _warning_text(caplog)
    assert FAKE_API_KEY not in logs, (
        "fake API key leaked in backend provider-order warning log"
    )
    assert "vendor-a-pro" not in logs, (
        "the provider block must not be reproduced; only the failure named"
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
    assert load_provider_5h_usage(target) == {}

    logs = _warning_text(caplog)
    assert FAKE_API_KEY not in logs
    assert REASON_NOT_A_DICT in logs


# ---------------------------------------------------------------------------
# The HTTP provider list
# ---------------------------------------------------------------------------


def test_unparseable_api_body_does_not_leak_secret(
    tmp_path, monkeypatch, caplog, real_cc_switch
):
    """A 200 whose body is truncated mid-secret must not echo the body.

    The HTTP leg reports the parse failure structurally. A body a proxy
    rewrote, or a CC Switch that half-wrote a response, reaches here as
    raw bytes — and the reason it carries is what sends the chain to the
    database.
    """
    caplog.set_level(logging.WARNING, logger="provider_order")
    monkeypatch.setenv("HOME", str(tmp_path))

    payload = {
        "providers": [
            {"id": "1", "name": "vendor-a-pro", "api_key": FAKE_API_KEY},
        ]
    }
    full = json.dumps(payload)
    _serve(monkeypatch, full[: len(full) // 2].encode())

    with pytest.raises(ProviderOrderError):
        provider_order.load_fallback_order()

    logs = _warning_text(caplog)
    assert FAKE_API_KEY not in logs, (
        "fake API key leaked from the HTTP leg's warning"
    )
    assert "vendor-a-pro" not in logs, (
        "the body must not be reproduced; only the classification"
    )


def test_only_the_name_crosses_out_of_a_provider_row(
    tmp_path, monkeypatch, real_cc_switch, caplog
):
    """Extra fields on a provider row are never echoed or retained.

    A CC Switch row can carry whatever the operator typed into it. Only
    ``name`` crosses into the chain, so a credential sharing a row with
    a usable provider has no path into the chain — and the HTTP leg
    succeeded, so nothing about the row was written down anywhere.
    """
    caplog.set_level(logging.WARNING, logger="provider_order")
    monkeypatch.setenv("HOME", str(tmp_path))
    _serve(
        monkeypatch,
        json.dumps(
            {"providers": [{"id": "1", "name": "vendor-a-pro",
                            "api_key": FAKE_API_KEY}]}
        ).encode(),
    )

    chain = provider_order.load_fallback_order()

    assert chain == ["vendor-a-pro"]
    assert FAKE_API_KEY not in repr(chain)
    assert FAKE_API_KEY not in _warning_text(caplog)
