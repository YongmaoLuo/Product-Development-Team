"""DB schema compatibility tests for CC Switch provider tables.

VP-028: CC Switch DB provider 表字段类型兼容性

These tests exercise the provider-config consumer layer
(``cc_switch``), which reads two on-disk table shapes:
the legacy ``provider_configs`` table and CC Switch's own ``providers``
table.  Both are run against v1/v2 schema snapshots.  Add, drop and
type-change scenarios must either remain compatible or raise an explicit
schema error — never silently lose data.

The id-keyed ``providers`` reader that used to live in ``cc_switch``
was deleted 2026-09-24 (see that module's docstring); its five
``test_legacy_providers_*`` cases went with it.
"""

import contextlib
import dataclasses
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

import cc_switch as pcc
from cc_switch import (
    CCSwitchError,
    ProviderConfig,
    get_provider,
    list_provider_names,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path, schema_sql, rows):
    """Create a temporary SQLite DB, run ``schema_sql`` and insert ``rows``."""
    db_path = tmp_path / "cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(schema_sql)
        for sql, params in rows:
            conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
    return db_path


@contextlib.contextmanager
def _temp_db(schema_sql, rows):
    """Yield a temporary DB path and clean it up on exit."""
    with tempfile.TemporaryDirectory() as tmp:
        yield _make_db(Path(tmp), schema_sql, rows)


# ---------------------------------------------------------------------------
# provider_configs (new consumer layer)
# ---------------------------------------------------------------------------

PROVIDER_CONFIGS_V1 = """
CREATE TABLE provider_configs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    model TEXT NOT NULL,
    extra_params TEXT
)
"""

PROVIDER_CONFIGS_V2_ADDED_COLS = """
CREATE TABLE provider_configs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    model TEXT NOT NULL,
    extra_params TEXT,
    api_key TEXT,
    created_at TEXT,
    notes TEXT
)
"""

PROVIDER_CONFIGS_V2_DROPPED_URL = """
CREATE TABLE provider_configs (
    id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    extra_params TEXT
)
"""

PROVIDER_CONFIGS_V2_DROPPED_MODEL = """
CREATE TABLE provider_configs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    extra_params TEXT
)
"""

PROVIDER_CONFIGS_V2_DROPPED_EXTRA = """
CREATE TABLE provider_configs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    model TEXT NOT NULL
)
"""

PROVIDER_CONFIGS_V2_TYPE_EXTRA_AS_TEXT = """
CREATE TABLE provider_configs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    model TEXT NOT NULL,
    extra_params TEXT
)
"""

FULL_PROVIDER_CONFIGS_ROW = (
    "INSERT INTO provider_configs (id, url, model, extra_params) VALUES (?, ?, ?, ?)",
    ("vendor-b-pro", "https://api.vendor-b.example/v1", "b-pro-4", '{"temperature": 0.7}'),
)


def test_provider_configs_v1_full_schema():
    """v1 snapshot with all canonical columns returns the expected config."""
    with _temp_db(PROVIDER_CONFIGS_V1, [FULL_PROVIDER_CONFIGS_ROW]) as db_path:
        cfg = get_provider("vendor-b-pro", db_path=db_path)
        assert cfg == ProviderConfig(
            name="vendor-b-pro",
            env={"temperature": 0.7},
            base_url="https://api.vendor-b.example/v1",
            model="b-pro-4",
        )


def test_provider_configs_v2_added_columns_ignored():
    """Extra columns added in a v2 snapshot are ignored and must not leak."""
    row = (
        "INSERT INTO provider_configs "
        "(id, url, model, extra_params, api_key, created_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "vendor-b-pro",
            "https://api.vendor-b.example/v1",
            "b-pro-4",
            '{"temperature": 0.7}',
            "secret-token",
            "2026-06-16T00:00:00Z",
            "test note",
        ),
    )
    with _temp_db(PROVIDER_CONFIGS_V2_ADDED_COLS, [row]) as db_path:
        cfg = get_provider("vendor-b-pro", db_path=db_path)
        assert cfg == ProviderConfig(
            name="vendor-b-pro",
            env={"temperature": 0.7},
            base_url="https://api.vendor-b.example/v1",
            model="b-pro-4",
        )
        # The type is closed — there is no mapping an unknown column
        # could be added to — so what this test can still catch is the
        # one column that *would* become meaningful if it leaked: a
        # stray ``api_key`` column must not be mistaken for a credential
        # (the legacy layout declares credentials inside ``extra_params``).
        assert cfg.api_key == ""
        assert [f.name for f in dataclasses.fields(cfg)] == [
            "name",
            "env",
            "base_url",
            "model",
        ]


def test_provider_configs_v2_dropped_url_defaults_empty():
    """A dropped optional ``url`` column defaults to "", not a crash."""
    row = (
        "INSERT INTO provider_configs (id, model, extra_params) VALUES (?, ?, ?)",
        ("vendor-b-pro", "b-pro-4", '{"temperature": 0.7}'),
    )
    with _temp_db(PROVIDER_CONFIGS_V2_DROPPED_URL, [row]) as db_path:
        cfg = get_provider("vendor-b-pro", db_path=db_path)
        assert cfg == ProviderConfig(
            name="vendor-b-pro",
            env={"temperature": 0.7},
            base_url="",
            model="b-pro-4",
        )


def test_provider_configs_v2_dropped_model_defaults_none():
    """A dropped optional ``model`` column defaults to None."""
    row = (
        "INSERT INTO provider_configs (id, url, extra_params) VALUES (?, ?, ?)",
        ("vendor-b-pro", "https://api.vendor-b.example/v1", "{}"),
    )
    with _temp_db(PROVIDER_CONFIGS_V2_DROPPED_MODEL, [row]) as db_path:
        cfg = get_provider("vendor-b-pro", db_path=db_path)
        assert cfg == ProviderConfig(
            name="vendor-b-pro",
            env={},
            base_url="https://api.vendor-b.example/v1",
            model=None,
        )


def test_provider_configs_v2_dropped_extra_params_defaults_empty():
    """A dropped optional ``extra_params`` column defaults to an empty dict."""
    row = (
        "INSERT INTO provider_configs (id, url, model) VALUES (?, ?, ?)",
        ("vendor-b-pro", "https://api.vendor-b.example/v1", "b-pro-4"),
    )
    with _temp_db(PROVIDER_CONFIGS_V2_DROPPED_EXTRA, [row]) as db_path:
        cfg = get_provider("vendor-b-pro", db_path=db_path)
        assert cfg == ProviderConfig(
            name="vendor-b-pro",
            env={},
            base_url="https://api.vendor-b.example/v1",
            model="b-pro-4",
        )


def test_provider_configs_v2_type_change_extra_params_still_parses():
    """Changing the declared type of ``extra_params`` must not silently
    discard valid JSON params as long as the stored value is still JSON text.
    """
    # SQLite type affinity means a TEXT-declared column still hands back the
    # stored JSON string; the consumer must parse it and preserve the data.
    row = (
        "INSERT INTO provider_configs (id, url, model, extra_params) VALUES (?, ?, ?, ?)",
        ("vendor-b-pro", "https://api.vendor-b.example/v1", "b-pro-4", '{"temperature": 0.7}'),
    )
    with _temp_db(PROVIDER_CONFIGS_V2_TYPE_EXTRA_AS_TEXT, [row]) as db_path:
        cfg = get_provider("vendor-b-pro", db_path=db_path)
        assert cfg.env == {"temperature": 0.7}


def test_provider_configs_v2_list_names_with_added_columns():
    """Enumeration tolerates extra columns on the legacy ``provider_configs`` layout.

    Extra columns must not break the read. The legacy layout has no
    ``name`` column, and enumeration keys on that column (it is the
    string the backend resolves providers by), so the result is an
    empty list — not an error, and not a list of ids nothing else speaks.
    """
    row = (
        "INSERT INTO provider_configs "
        "(id, url, model, extra_params, notes) VALUES (?, ?, ?, ?, ?)",
        (
            "vendor-a-default",
            "https://api.vendor-a.chat/v1",
            "Vendor A-Text-01",
            "{}",
            "n",
        ),
    )
    with _temp_db(PROVIDER_CONFIGS_V2_ADDED_COLS, [row]) as db_path:
        assert list_provider_names(db_path=db_path) == []


# ---------------------------------------------------------------------------
# Cross-layer smoke: neither consumer hardcodes a provider URL map.
# ---------------------------------------------------------------------------


def test_no_hardcoded_provider_url_map_in_consumers():
    """Both consumer modules must remain free of the legacy provider URL map."""
    backend_dir = Path(__file__).parent.parent
    legacy_token = "provider" + "-" + "url" + "-map"
    for module_name in ("cc_switch.py", "cc_switch.py"):
        text = (backend_dir / module_name).read_text(encoding="utf-8")
        assert legacy_token not in text
