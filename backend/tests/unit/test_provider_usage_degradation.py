"""TDD tests for the usage readers' *degradation reporting*.

``load_provider_5h_usage`` answers a question whose two failure modes
look identical from the outside: "the producer has not written the file
yet" and "the file is there but says nothing about this provider" both
arrive as an empty map, and the dispatch path reads an empty map as
"every provider is fully available". The reader therefore also reports
*why* it degraded:

  * A failed read is **not cached** — the next call reads the file
    again, so a payload the producer writes a moment later is picked up
    without an operator clearing anything.
  * :func:`provider_order.last_usage_read_reason` (and its quota
    sibling) name the reason of the last read, so a caller can tell
    "unreadable" from "absent" instead of guessing from an empty map.

The vocabulary is fixed:

  ``file_missing``    the file is not there
  ``parse_error``     the file is not JSON
  ``invalid_shape``   the file parsed but is not a provider table
  ``provider_absent`` the table was read; it does not hold this provider
  ``None``            the read did not degrade

Nothing here names a machine: the path is always a ``tmp_path`` file or
the ``PDT_PROVIDER_USAGE_FILE`` override, and the quota leg is stubbed
at ``urlopen`` so no CC Switch is contacted.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

import provider_order
from provider_order import (
    REASON_FILE_MISSING,
    REASON_INVALID_SHAPE,
    REASON_PARSE_ERROR,
    REASON_PERMISSION_DENIED,
    REASON_PROVIDER_ABSENT,
    REASON_QUOTA_UNREACHABLE,
    cache_clear,
    last_quota_read_reason,
    last_usage_read_reason,
    load_provider_5h_usage,
    load_provider_quota_map,
)

#: Root bypasses file permissions, so the "cannot open it" case cannot
#: be staged as root — the read succeeds and the reason is not the one
#: under test.
_RUNNING_AS_ROOT = getattr(os, "geteuid", lambda: 1)() == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_payload(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _table(*provider_ids: str) -> dict:
    """A payload whose ``providers`` table holds the given ids."""
    return {
        "version": 1,
        "providers": {
            pid: {"five_hour_remaining_pct": 50.0} for pid in provider_ids
        },
    }


class _FakeResponse:
    """A 200 with a body — the shape :func:`_quota_from_api` expects."""

    def __init__(self, body: bytes):
        self._body = body
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return 200

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


@pytest.fixture(autouse=True)
def _fresh_caches():
    """Each test starts with an empty cache and an empty reason."""
    cache_clear()
    yield
    cache_clear()


# ---------------------------------------------------------------------------
# 1. A failed read is not cached
# ---------------------------------------------------------------------------


def test_missing_file_is_not_cached(tmp_path):
    """A file that is not there must not be remembered as "empty".

    The producer writes the payload on its own schedule. A reader that
    cached the absence would keep reporting "no quota data" for the
    life of the process, long after the file appeared.
    """
    target = tmp_path / "provider-order.json"

    assert load_provider_5h_usage(target) == {}

    _write_payload(target, _table("vendor-a-pro"))

    # No cache_clear() in between: the second read must go to the disk.
    assert load_provider_5h_usage(target) == {"vendor-a-pro": 50.0}


def test_a_later_produced_file_is_picked_up_without_cache_clear(
    tmp_path, monkeypatch
):
    """The default-path leg behaves the same way.

    ``PDT_PROVIDER_USAGE_FILE`` is the only seam for the payload's
    location — no path is written down here — so the test points it at
    a file that does not exist yet.
    """
    target = tmp_path / "provider-order.json"
    monkeypatch.setenv("PDT_PROVIDER_USAGE_FILE", str(target))

    assert load_provider_5h_usage() == {}

    _write_payload(target, _table("vendor-a-pro"))

    assert load_provider_5h_usage() == {"vendor-a-pro": 50.0}
    assert last_usage_read_reason() is None


def test_a_malformed_file_is_not_cached(tmp_path):
    """Garbage on disk is a failed read too, and fails the same way."""
    target = tmp_path / "provider-order.json"
    target.write_text("{not valid json", encoding="utf-8")

    assert load_provider_5h_usage(target) == {}

    _write_payload(target, _table("vendor-a-pro"))

    assert load_provider_5h_usage(target) == {"vendor-a-pro": 50.0}


def test_a_file_of_the_wrong_shape_is_not_cached(tmp_path):
    """A payload that is not a provider table is a failed read."""
    target = tmp_path / "provider-order.json"
    _write_payload(target, {"providers": ["vendor-a-pro"]})

    assert load_provider_5h_usage(target) == {}

    _write_payload(target, _table("vendor-a-pro"))

    assert load_provider_5h_usage(target) == {"vendor-a-pro": 50.0}


# ---------------------------------------------------------------------------
# 2. The reason of the last read is queryable
# ---------------------------------------------------------------------------


def test_missing_file_reason_is_queryable(tmp_path):
    """The reason of the last read is a machine-readable token."""
    target = tmp_path / "provider-order.json"

    load_provider_5h_usage(target)

    assert last_usage_read_reason() == REASON_FILE_MISSING


def test_parse_error_reason(tmp_path):
    """A file that is not JSON says so, and does not say "missing"."""
    target = tmp_path / "provider-order.json"
    target.write_text("{not valid json", encoding="utf-8")

    load_provider_5h_usage(target)

    assert last_usage_read_reason() == REASON_PARSE_ERROR
    assert last_usage_read_reason() != REASON_FILE_MISSING


@pytest.mark.skipif(_RUNNING_AS_ROOT, reason="root bypasses file permissions")
def test_permission_denied_reason(tmp_path):
    """A file that cannot be opened says so — it is not "missing".

    The file is there and the operator can see it; what failed is the
    read. An ACL problem and an absent file are different problems
    with different fixes, so they must not report the same reason.
    """
    target = tmp_path / "provider-order.json"
    _write_payload(target, _table("vendor-a-pro"))
    target.chmod(0o000)
    try:
        load_provider_5h_usage(target)

        assert last_usage_read_reason() == REASON_PERMISSION_DENIED
        assert last_usage_read_reason() != REASON_FILE_MISSING
    finally:
        target.chmod(0o600)


def test_invalid_shape_reason(tmp_path):
    """A parsed file that is not a table says so."""
    target = tmp_path / "provider-order.json"
    _write_payload(target, ["vendor-a-pro"])

    load_provider_5h_usage(target)

    assert last_usage_read_reason() == REASON_INVALID_SHAPE


def test_success_clears_reason(tmp_path):
    """A good read replaces the previous failure's reason."""
    target = tmp_path / "provider-order.json"

    load_provider_5h_usage(target)
    assert last_usage_read_reason() == REASON_FILE_MISSING

    _write_payload(target, _table("vendor-a-pro"))
    assert load_provider_5h_usage(target) == {"vendor-a-pro": 50.0}

    assert last_usage_read_reason() is None


def test_provider_absent_is_distinct_from_unreadable(tmp_path):
    """"Not in the table" and "cannot read the table" are not one fact.

    The first means the producer has no reading for this provider; the
    second means there is no table at all to read it from. A caller
    that conflates them either retries forever or gives up on a
    provider that was never reported.
    """
    target = tmp_path / "provider-order.json"
    _write_payload(target, _table("vendor-a-pro"))

    load_provider_5h_usage(target)

    # The table was read: the provider in it has no degradation.
    assert last_usage_read_reason("vendor-a-pro") is None
    # The table was read: the provider *not* in it is absent.
    assert last_usage_read_reason("vendor-b") == REASON_PROVIDER_ABSENT

    # Now the table cannot be read at all — that is the other fact.
    missing = tmp_path / "not-there.json"
    load_provider_5h_usage(missing)

    assert last_usage_read_reason("vendor-b") == REASON_FILE_MISSING
    assert last_usage_read_reason("vendor-b") != REASON_PROVIDER_ABSENT


def test_the_reason_is_not_mutable_by_the_caller(tmp_path):
    """The reason is a token, not a shared object a caller can edit."""
    target = tmp_path / "provider-order.json"

    load_provider_5h_usage(target)
    reason = last_usage_read_reason()

    assert isinstance(reason, str)
    assert last_usage_read_reason() == reason


def test_concurrent_readers_agree_on_the_reason(tmp_path):
    """The reason is written under a lock, so it is never half-set."""
    target = tmp_path / "provider-order.json"
    _write_payload(target, _table("vendor-a-pro"))
    errors: list = []

    def _reader() -> None:
        try:
            for _ in range(20):
                load_provider_5h_usage(target)
                last_usage_read_reason("vendor-b")
        except Exception as exc:  # pragma: no cover - the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert last_usage_read_reason() is None


# ---------------------------------------------------------------------------
# 3. The quota leg reports its own reason
# ---------------------------------------------------------------------------


def test_quota_read_reason_records_an_unreachable_cc_switch(monkeypatch):
    """CC Switch being down is a reason, not an empty map with no story."""

    def _boom(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(provider_order.urllib.request, "urlopen", _boom)

    assert load_provider_quota_map(force=True) == {}

    assert last_quota_read_reason() == REASON_QUOTA_UNREACHABLE


def test_quota_read_reason_is_none_after_a_successful_fetch(monkeypatch):
    """A good fetch degrades nothing."""
    body = json.dumps(
        {
            "providers": [
                {
                    "provider": "vendor-a-pro",
                    "success": True,
                    "tiers": [
                        {
                            "name": "five_hour",
                            "used_percent": 10.0,
                            "remaining": 90.0,
                            "total": 100.0,
                            "unit": "%",
                            "resets_at": None,
                        }
                    ],
                }
            ]
        }
    ).encode("utf-8")

    def _urlopen(request, timeout=None):
        return _FakeResponse(body)

    monkeypatch.setattr(provider_order.urllib.request, "urlopen", _urlopen)

    assert "vendor-a-pro" in load_provider_quota_map(force=True)

    assert last_quota_read_reason() is None
    assert last_quota_read_reason("vendor-a-pro") is None
    assert last_quota_read_reason("vendor-b") == REASON_PROVIDER_ABSENT


def test_quota_provider_absent_is_distinct_from_a_failed_fetch(monkeypatch):
    """The quota leg draws the same line as the file leg."""

    def _boom(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(provider_order.urllib.request, "urlopen", _boom)

    load_provider_quota_map(force=True)

    assert last_quota_read_reason("vendor-b") == REASON_QUOTA_UNREACHABLE
    assert last_quota_read_reason("vendor-b") != REASON_PROVIDER_ABSENT


# ---------------------------------------------------------------------------
# 4. The payload reader keeps its old shape
# ---------------------------------------------------------------------------


def test_read_payload_still_returns_the_payload(tmp_path):
    """``_read_payload`` stays callable for callers that want the payload.

    The reason-aware form is internal; the old entry point keeps
    returning the parsed dict (or ``None``), so the callers that only
    want the payload do not have to change.
    """
    target = tmp_path / "provider-order.json"
    _write_payload(target, _table("vendor-a-pro"))

    payload = provider_order._read_payload(target)

    assert payload == _table("vendor-a-pro")
    assert provider_order._read_payload(tmp_path / "gone.json") is None
