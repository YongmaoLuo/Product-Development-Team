"""VP-008 entry point for provider_order degradation scenarios.

The bulk of the test matrix lives in ``backend/tests/test_provider_order.py``.
This file re-exports those tests so the verification ``test_command``
(``pytest backend/tests/test_provider_order_degradation.py``) resolves,
and additionally pins the two VP-008 scenarios that are not exercised
by the existing parametrized suite:

  * ``permission_denied``  — ``Path.open`` raises ``PermissionError``
  * ``invalid_order_type`` — ``order`` present but not a ``list``

VP-008 contract coverage (8 degradation scenarios, all MUST raise
``ProviderOrderError`` and emit a structured WARNING with
``reason=<sentinel>``):

  * ``file_missing``           — covered by re-exported
    ``test_yaml_unavailable_falls_back_to_hardcoded[file_missing-missing]``
  * ``permission_denied``      — covered by ``test_permission_denied`` below
  * ``json_invalid``           — covered by re-exported
    ``test_yaml_unavailable_falls_back_to_hardcoded[json_invalid-malformed]``
  * ``version_mismatch``       — covered by re-exported
    ``test_yaml_unavailable_falls_back_to_hardcoded[version_mismatch-bad_version]``
  * ``order_missing``          — covered by re-exported
    ``test_yaml_unavailable_falls_back_to_hardcoded[order_missing-order_missing]``
  * ``order_empty``            — covered by re-exported
    ``test_yaml_unavailable_falls_back_to_hardcoded[order_empty-order_empty]``
  * ``invalid_order_type``     — covered by ``test_invalid_order_type`` below
  * ``updated_at_invalid``     — covered by re-exported
    ``test_yaml_unavailable_falls_back_to_hardcoded[updated_at_invalid-bad_timestamp]``

The lru_cache contract (10 same-process calls → exactly 1 file IO)
is pinned by ``test_lru_cache``, re-exported below.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from provider_order import (
    REASON_INVALID_ORDER_TYPE,
    REASON_PERMISSION_DENIED,
    SCHEMA_VERSION,
    ProviderOrderError,
    cache_clear,
    load_fallback_order,
)

# Re-export the bulk of the VP-008 matrix from the canonical test module.
from backend.tests.test_provider_order import *  # noqa: F401,F403,E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _write_payload(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _capture_warning(caplog: pytest.LogCaptureFixture):
    return [
        rec for rec in caplog.records
        if rec.name == "provider_order" and rec.levelno == logging.WARNING
    ]


def _make_fake_cc_switch_db(tmp_path: Path, provider_ids):
    import sqlite3

    db_path = tmp_path / "cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE provider_configs ("
            "id TEXT PRIMARY KEY, url TEXT, model TEXT, extra_params TEXT"
            ")"
        )
        for pid in provider_ids:
            conn.execute(
                "INSERT INTO provider_configs (id, url, model, extra_params) "
                "VALUES (?, ?, ?, ?)",
                (pid, "https://example.com/v1", "fake-model", "{}"),
            )
        conn.commit()
    finally:
        conn.close()

    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path.replace(cc_dir / "cc-switch.db")
    return cc_dir / "cc-switch.db"


def test_permission_denied(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VP-008: ``PermissionError`` on read -> ProviderOrderError + WARNING(reason=permission_denied).

    The implementation catches ``PermissionError`` *before* the generic
    ``OSError`` handler (see ``provider_order._read_payload``) so that
    ACL-related failures can be grepped by operators. This test pins
    that path without depending on filesystem ACLs (which would be
    ineffective under root): we monkeypatch ``Path.open`` to raise
    ``PermissionError`` for the target file only.
    """
    cache_clear()
    caplog.set_level(logging.WARNING, logger="provider_order")

    _make_fake_cc_switch_db(tmp_path, ["vendor-a-pro", "vendor-b-pro"])
    monkeypatch.setenv("HOME", str(tmp_path))

    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["vendor-a-pro", "vendor-b-pro"],
        "providers": {},
    }
    _write_payload(target, payload)

    real_open = Path.open

    def denying_open(self, *args, **kwargs):
        if Path(self).resolve() == target.resolve():
            raise PermissionError("simulated ACL denial")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denying_open)

    with pytest.raises(ProviderOrderError):
        load_fallback_order(target)

    warnings = _capture_warning(caplog)
    assert any(
        getattr(rec, "reason", None) == REASON_PERMISSION_DENIED
        for rec in warnings
    ), (
        f"expected a WARNING with reason={REASON_PERMISSION_DENIED!r} on the "
        f"provider_order logger, got reasons="
        f"{[getattr(r, 'reason', None) for r in warnings]!r}"
    )


def test_invalid_order_type(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VP-008: ``order`` present but not a list -> ProviderOrderError + WARNING(reason=invalid_order_type).

    Distinct from ``order_missing`` (key absent) and ``order_empty``
    (key is ``[]``): the key is present but holds the wrong type
    (string, dict, int). The implementation emits a dedicated
    ``invalid_order_type`` reason so operators can grep for this
    specific shape.
    """
    cache_clear()
    caplog.set_level(logging.WARNING, logger="provider_order")

    _make_fake_cc_switch_db(tmp_path, ["vendor-a-pro", "vendor-b-pro"])
    monkeypatch.setenv("HOME", str(tmp_path))

    target = tmp_path / "provider-order.json"

    # Each shape is a distinct contract violation.
    bad_order_values = [
        "vendor-a-pro,vendor-b-pro",   # string instead of list
        {"vendor-a-pro": 1},        # dict instead of list
        42,                           # int instead of list
    ]

    for bad in bad_order_values:
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": _now_iso(),
            "source": "producer",
            "order": bad,
            "providers": {},
        }
        _write_payload(target, payload)
        caplog.clear()
        cache_clear()

        with pytest.raises(ProviderOrderError):
            load_fallback_order(target)

        warnings = _capture_warning(caplog)
        assert any(
            getattr(rec, "reason", None) == REASON_INVALID_ORDER_TYPE
            for rec in warnings
        ), (
            f"order={bad!r} must surface reason={REASON_INVALID_ORDER_TYPE!r}; "
            f"got reasons={[getattr(r, 'reason', None) for r in warnings]!r}"
        )
