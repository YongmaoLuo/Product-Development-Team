"""VP-021 — an unknown provider name must not crash the backend.

The chain is CC Switch's list, verbatim. That means it can name a
provider this backend cannot resolve — a provider configured for a
different app, or one whose row CC Switch has but the consumer layer
cannot parse. The contract this module pins is **where** that is
handled:

* **not** in the chain reader, which mirrors what CC Switch said and
  applies no membership filter of its own; and
* **yes** in :func:`server.load_providers`, which resolves each entry
  and skips one it cannot resolve, with a warning naming it.

Skipping rather than failing is deliberate: one unusable row must not
take down a chain whose other entries are fine. Silently skipping is
not acceptable either, so the skip is always logged.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import List

import pytest

import cc_switch as pcc
from cc_switch import (
    get_provider,
    list_provider_names,
)
from provider_order import (
    cache_clear,
    load_fallback_order,
)


#: An authority nothing can be listening on, so the HTTP leg fails the
#: way it does when CC Switch is closed and the database has to answer.
API_DOWN = "http://127.0.0.1:1"


def _make_fake_cc_switch_db(db_path: Path, provider_names: List[str]) -> Path:
    """Create a fake CC Switch DB declaring *provider_names*.

    Production ``providers`` schema including ``app_type`` and the
    ordering columns: the chain reader filters on the app type and sorts
    by them, so a fixture without those would not exercise the query it
    is standing in for.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS providers ("
            "id TEXT NOT NULL, app_type TEXT NOT NULL, name TEXT NOT NULL, "
            "settings_config TEXT NOT NULL, created_at INTEGER, "
            "sort_index INTEGER, is_current BOOLEAN NOT NULL DEFAULT 0, "
            "PRIMARY KEY (id, app_type))"
        )
        for i, name in enumerate(provider_names):
            settings = {
                "env": {
                    "ANTHROPIC_BASE_URL": f"https://example.com/p{i}/v1",
                    "ANTHROPIC_MODEL": f"fake-model-{i}",
                }
            }
            conn.execute(
                "INSERT OR REPLACE INTO providers (id, app_type, name, "
                "settings_config, created_at, sort_index) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"row-{i}", "claude", name, json.dumps(settings), i, i),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _install_fake_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    known_names: List[str],
) -> Path:
    cc_dir = tmp_path / ".cc-switch"
    db_path = _make_fake_cc_switch_db(cc_dir / "cc-switch.db", known_names)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", API_DOWN)
    return db_path


def _stub_api(monkeypatch, names):
    """Make CC Switch's HTTP provider list answer with ``names``."""
    body = json.dumps(
        {"providers": [{"id": f"id-{i}", "name": n} for i, n in enumerate(names)]}
    ).encode()

    class _Resp:
        status = 200

        def read(self):
            return body

        def getcode(self):
            return self.status

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import provider_order

    monkeypatch.setattr(
        provider_order.urllib.request, "urlopen", lambda *a, **k: _Resp()
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


# Scenario 2 — the chain reader does not filter


def test_chain_reader_applies_no_membership_filter(
    tmp_path, monkeypatch, real_cc_switch
):
    """Every name CC Switch listed is in the chain, resolvable or not.

    The reader used to drop entries with no matching database row. That
    filter made the chain a function of the database as well as of CC
    Switch, which is how a chain could disagree with the application
    the operator is looking at — and it would drop exactly the newest
    row, the one CC Switch has and the consumer has not seen yet.
    """
    cache_clear()
    _install_fake_home(tmp_path, monkeypatch, ["Vendor B Pro", "Vendor A Pro"])
    _stub_api(monkeypatch, ["Vendor B Pro", "futurecorp-neo", "Vendor A Pro"])

    assert load_fallback_order() == [
        "Vendor B Pro",
        "futurecorp-neo",
        "Vendor A Pro",
    ]


def test_chain_reader_returns_a_chain_of_only_unresolvable_names(
    tmp_path, monkeypatch, real_cc_switch
):
    """Names the consumer cannot resolve are still a chain.

    ``[]`` from this reader would be indistinguishable from "no
    providers configured", and the reader has no way to tell the two
    apart without consulting a source it is supposed to be mirroring.
    Reporting the names and letting the consumer skip them keeps the
    decision in one place, where it can be logged.
    """
    cache_clear()
    _install_fake_home(tmp_path, monkeypatch, ["Vendor B Pro"])
    _stub_api(monkeypatch, ["alpha-unknown", "beta-unknown", "gamma-unknown"])

    assert load_fallback_order() == [
        "alpha-unknown",
        "beta-unknown",
        "gamma-unknown",
    ]


def test_chain_reader_reports_a_realistic_mixed_chain(
    tmp_path, monkeypatch, real_cc_switch
):
    """A chain mixing known and unknown names comes back whole."""
    cache_clear()
    _install_fake_home(tmp_path, monkeypatch, ["Vendor B Pro", "Vendor A Pro"])
    order = [
        "Vendor B Pro",
        "vendor-c-app",
        "Vendor A Pro",
        "vendor-c-v2",
        "tencent-hunyuan",
        "vendor-d-chat",
    ]
    _stub_api(monkeypatch, order)

    assert load_fallback_order() == order


# Scenario 3 — server.load_providers skips unknown names at startup


def test_load_providers_skips_unknown_ids(tmp_path, monkeypatch, caplog, real_cc_switch):
    """The skip happens here, and it is logged.

    A chain entry with no resolvable row is dropped so the rest of the
    chain stays dispatchable, and the warning names the dropped entries
    — an operator whose provider silently stopped being used needs to
    be able to find out from the log.
    """
    cache_clear()
    caplog.set_level(logging.WARNING)

    _install_fake_home(tmp_path, monkeypatch, ["Vendor B Pro", "Vendor A Pro"])
    _stub_api(
        monkeypatch,
        ["Vendor B Pro", "ghost-unknown-1", "Vendor A Pro", "ghost-unknown-2"],
    )

    import server

    providers = server.load_providers()
    provider_names = [cfg.name for cfg in providers]

    assert "Vendor B Pro" in provider_names
    assert "Vendor A Pro" in provider_names
    assert "ghost-unknown-1" not in provider_names
    assert "ghost-unknown-2" not in provider_names
    # The surviving order is the chain's order, with the gaps closed up.
    assert provider_names == ["Vendor B Pro", "Vendor A Pro"]

    warning_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert (
        "ghost-unknown-1" in warning_text
        or "unknown provider" in warning_text.lower()
    ), f"startup must log that unknown IDs were skipped; got: {warning_text!r}"


def test_load_providers_with_only_unresolvable_names_yields_an_empty_set(
    tmp_path, monkeypatch, caplog, real_cc_switch
):
    """Every entry skipped leaves an empty provider set, loudly.

    Startup does not raise here. It did not before either: the reader
    used to filter unknown names out, which made the chain itself
    empty, and ``load_providers`` warns and returns ``[]`` for an empty
    chain. The observable outcome is unchanged.

    What this pins is that the outcome is still *attributable* — every
    skipped name is named in a warning. An empty provider set is a
    state an operator has to be able to diagnose from the log, because
    the process comes up and looks healthy.
    """
    cache_clear()
    caplog.set_level(logging.WARNING)

    _install_fake_home(tmp_path, monkeypatch, ["Vendor B Pro"])
    _stub_api(monkeypatch, ["alpha-unknown", "beta-unknown"])

    import server

    assert server.load_providers() == []

    warning_text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "alpha-unknown" in warning_text
    assert "beta-unknown" in warning_text, (
        f"every skipped name must be named in the log; got: {warning_text!r}"
    )


# Scenario 4 — kebab validation does not reject well-formed unknown IDs


@pytest.mark.parametrize(
    "unknown_id",
    ["unknown-vendor", "ghost-provider", "future-llm"],
)
def test_list_provider_ids_excludes_unknown_ids(tmp_path, unknown_id):
    db_path = _make_fake_cc_switch_db(tmp_path / "cc-switch.db", ["Vendor B Pro"])
    ids = list_provider_names(db_path=db_path)
    assert unknown_id not in ids
    assert ids == ["Vendor B Pro"]
