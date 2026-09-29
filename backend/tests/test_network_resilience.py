"""VP-027 — Network resilience contract tests (the backend call chain).

The verification point pins the following contract for the backend's
provider-call surfaces:

  * connection timeout          → degrade, no unhandled exception
  * HTTP 500                    → degrade, no unhandled exception
  * ConnectionRefused           → degrade, no unhandled exception
  * DNS resolution failure      → degrade, no unhandled exception

The production surface under test is:

  * ``ClaudeCodingTool._check_provider_availability(provider_name)``
    in ``backend/coding_tool.py`` — probes ``{base_url}/v1/models`` and
    returns ``(False, {})`` on any transport error.

    A second surface was removed when the vendor-b peak-hour policy moved
    into the producer's rule engine: the backend consumes
    published verdicts now and no longer reads CC Switch usage itself.

The surface is designed to swallow transport errors at the boundary.
These tests verify it never propagates unhandled exceptions for any of
the four failure modes.

The HTTP layer is mocked via ``monkeypatch`` on
``urllib.request.urlopen`` so no real network traffic is generated.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any
from unittest.mock import MagicMock

import pytest

from coding_tool import ClaudeCodingTool


# ---------------------------------------------------------------------------
# Fakes & helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Stand-in for ``http.client.HTTPResponse``.

    Only ``read()``, ``status``, and the context-manager protocol are
    exercised by the production code paths under test.
    """

    def __init__(self, body: Any, status: int = 200) -> None:
        if isinstance(body, (bytes, bytearray)):
            self._body: bytes = bytes(body)
        elif isinstance(body, str):
            self._body = body.encode("utf-8")
        else:
            self._body = json.dumps(body).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False


def _write_cc_switch_db(tmp_path, providers: dict) -> None:
    """Write a fake cc-switch SQLite DB with the given provider rows.

    ``providers`` maps cc-switch ``name`` (e.g. ``"Vendor B Pro"``) to the
    env block landed in the row's ``settings_config``. The schema
    mirrors the real cc-switch ``providers`` table used by the backend
    test suite.
    """
    import sqlite3

    db_dir = tmp_path / ".cc-switch"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE providers (
                id TEXT NOT NULL,
                app_type TEXT NOT NULL,
                name TEXT NOT NULL,
                settings_config TEXT NOT NULL,
                website_url TEXT,
                category TEXT,
                created_at INTEGER,
                sort_index INTEGER,
                notes TEXT,
                icon TEXT,
                icon_color TEXT,
                meta TEXT NOT NULL DEFAULT '{}',
                is_current BOOLEAN NOT NULL DEFAULT 0,
                in_failover_queue BOOLEAN NOT NULL DEFAULT 0,
                cost_multiplier TEXT NOT NULL DEFAULT '1.0',
                limit_daily_usd TEXT,
                limit_monthly_usd TEXT,
                provider_type TEXT,
                PRIMARY KEY (id, app_type)
            )
            """
        )
        for i, (cc_name, env) in enumerate(providers.items()):
            settings_config = json.dumps({"env": env})
            conn.execute(
                """
                INSERT INTO providers
                    (id, app_type, name, settings_config, meta, is_current,
                     in_failover_queue, cost_multiplier)
                VALUES (?, ?, ?, ?, '{}', 0, 0, '1.0')
                """,
                (f"id-{i}", "claude", cc_name, settings_config),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def vendor_b_in_db(tmp_path, monkeypatch):
    """Install a fake cc-switch DB row for the vendor-b-pro provider.

    Redirects ``Path.home()`` so the production code resolves the
    fake DB under ``tmp_path/.cc-switch/cc-switch.db``.
    """
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_cc_switch_db(
        tmp_path,
        {
            "Vendor B Pro": {
                "ANTHROPIC_BASE_URL": "https://api.vendor-b.example/anthropic",
                "ANTHROPIC_AUTH_TOKEN": "test-api-key",
            }
        },
    )


# ---------------------------------------------------------------------------
# Failure-mode constructors
# ---------------------------------------------------------------------------


def _timeout_urllib_error() -> urllib.error.URLError:
    """``urllib.error.URLError`` wrapping ``socket.timeout`` — the typical
    surface raised by ``urllib.request.urlopen`` on a request timeout.
    """
    return urllib.error.URLError(socket.timeout("timed out"))


def _refused_urllib_error() -> urllib.error.URLError:
    """``urllib.error.URLError`` wrapping ``ConnectionRefusedError`` — the
    typical surface raised when no process listens on the target port.
    """
    return urllib.error.URLError(ConnectionRefusedError("refused"))


def _dns_urllib_error() -> urllib.error.URLError:
    """``urllib.error.URLError`` wrapping ``socket.gaierror`` — the
    typical surface raised when DNS resolution fails.
    """
    return urllib.error.URLError(socket.gaierror("nodename nor servname provided"))


def _http_500_error() -> urllib.error.HTTPError:
    """``urllib.error.HTTPError`` with ``code=500`` — ``urlopen`` raises
    this for any non-2xx response.
    """
    return urllib.error.HTTPError(
        url="http://test-cc-switch.local/api/usage",
        code=500,
        msg="Internal Server Error",
        hdrs=None,
        fp=None,
    )


# ---------------------------------------------------------------------------
# Surface 1: ClaudeCodingTool._check_provider_availability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_factory,label",
    [
        (_timeout_urllib_error, "timeout"),
        (_http_500_error, "http_500"),
        (_refused_urllib_error, "connection_refused"),
        (_dns_urllib_error, "dns_failure"),
    ],
    ids=["timeout", "http_500", "connection_refused", "dns_failure"],
)
def test_check_provider_availability_degrades_on_network_error(
    vendor_b_in_db, monkeypatch, exc_factory, label,
):
    """``_check_provider_availability`` must return ``(False, {})`` for
    each network failure mode (timeout, HTTP 500, connection refused,
    DNS failure), never propagating the exception to the caller.

    This is the entry point the provider-selection loop calls before
    routing a request to a provider; an unhandled exception here would
    crash the agent run. The contract is: any transport-layer failure
    ⇒ ``(False, {})`` (provider is treated as unavailable).
    """
    exc = exc_factory()

    def _fake_urlopen(req, *args, **kwargs):
        raise exc

    monkeypatch.setattr(
        "urllib.request.urlopen", _fake_urlopen, raising=False,
    )

    available, config = ClaudeCodingTool._check_provider_availability("vendor-b-pro")

    assert available is False, (
        f"availability should be False under network failure {label!r}; "
        f"got available={available!r}"
    )
    assert config == {}, (
        f"config dict should be empty under network failure {label!r}; "
        f"got config={config!r}"
    )


# ---------------------------------------------------------------------------
# Boundary: bare-exception surfaces (no urllib wrapping)
# ---------------------------------------------------------------------------
#
# ``_check_provider_availability`` catches a bare ``Exception`` at the
# outer boundary, so even a raw ``socket.timeout`` /
# ``ConnectionRefusedError`` / ``OSError(ECONNREFUSED)`` (the kind of
# error some Python versions surface from the kernel without wrapping
# in URLError) must not propagate.


@pytest.mark.parametrize(
    "exc,label",
    [
        (socket.timeout("timed out"), "bare_socket_timeout"),
        (ConnectionRefusedError(61, "Connection refused"), "bare_refused"),
        (TimeoutError("timed out"), "bare_timeout_error"),
        (socket.gaierror("dns failure"), "bare_gaierror"),
    ],
    ids=[
        "bare_socket_timeout",
        "bare_refused",
        "bare_timeout_error",
        "bare_gaierror",
    ],
)
def test_check_provider_availability_swallows_bare_exception(
    vendor_b_in_db, monkeypatch, exc, label,
):
    """Bare exception surfaces (no ``URLError`` wrapping) must also be
    caught by the outer ``except Exception`` in
    ``_check_provider_availability``.

    Different Python versions surface transport errors differently:
    Python 3.9 raises ``socket.timeout`` / ``ConnectionRefusedError``
    directly when ``urlopen`` propagates from the socket layer without
    wrapping. The production code's outer ``except Exception: pass``
    must catch every variant and degrade to ``(False, {})``.
    """
    def _fake_urlopen(req, *args, **kwargs):
        raise exc

    monkeypatch.setattr(
        "urllib.request.urlopen", _fake_urlopen, raising=False,
    )

    available, config = ClaudeCodingTool._check_provider_availability("vendor-b-pro")
    assert available is False, (
        f"bare {label!r} should degrade to (False, {{}}); "
        f"got available={available!r}"
    )
    assert config == {}


