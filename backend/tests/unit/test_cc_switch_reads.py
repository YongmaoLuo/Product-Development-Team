"""Unit tests for ``backend/cc_switch.py``."""

import json
import sqlite3

import pytest

import cc_switch as pcc
from cc_switch import (
    CCSwitchError,
    ProviderConfig,
    current_provider,
    get_provider,
    list_provider_names,
)


def _make_fake_db(tmp_path):
    """Create a fake cc-switch.db with the provider_configs table."""
    db_path = tmp_path / "cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE provider_configs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                model TEXT NOT NULL,
                extra_params TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO provider_configs (id, url, model, extra_params)
            VALUES (?, ?, ?, ?)
            """,
            (
                "Vendor B Pro",
                "https://api.vendor-b.example/v1",
                "b-pro-4",
                '{"temperature": 0.7}',
            ),
        )
        conn.execute(
            """
            INSERT INTO provider_configs (id, url, model, extra_params)
            VALUES (?, ?, ?, ?)
            """,
            (
                "Vendor A Default",
                "https://api.vendor-a.chat/v1",
                "Vendor A-Text-01",
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_get_provider_config_ok(tmp_path):
    db_path = _make_fake_db(tmp_path)
    cfg = get_provider("Vendor B Pro", db_path=db_path)
    assert cfg == ProviderConfig(
        name="Vendor B Pro",
        env={"temperature": 0.7},
        base_url="https://api.vendor-b.example/v1",
        model="b-pro-4",
    )


def test_missing_provider_returns_none(tmp_path):
    db_path = _make_fake_db(tmp_path)
    assert get_provider("not-there", db_path=db_path) is None


def test_list_provider_names_legacy_schema(tmp_path):
    """The legacy ``provider_configs`` layout offers no provider names.

    Enumeration answers "which providers exist?" by reading the
    ``providers.name`` column — the string the rest of the system
    resolves providers by, since the kebab-case id layer was removed.
    A table with no such column cannot answer that question, so an
    empty list is the honest result rather than a list of ids nobody
    else speaks.
    """
    db_path = _make_fake_db(tmp_path)
    assert list_provider_names(db_path=db_path) == []


def test_missing_database_raises(tmp_path):
    missing = tmp_path / "does-not-exist.db"
    with pytest.raises(CCSwitchError):
        get_provider("Vendor B Pro", db_path=missing)


def test_invalid_database_file_raises(tmp_path):
    bad = tmp_path / "not-a-db.db"
    bad.write_text("this is not sqlite")
    with pytest.raises(CCSwitchError):
        get_provider("Vendor B Pro", db_path=bad)


def test_db_missing_raises(tmp_path):
    missing = tmp_path / "does-not-exist.db"
    with pytest.raises(CCSwitchError):
        get_provider("Vendor B Pro", db_path=missing)


def test_provider_not_found_returns_none_or_raises(tmp_path):
    """Missing rows return ``None`` (consumer-layer contract)."""
    db_path = _make_fake_db(tmp_path)
    assert get_provider("not-there", db_path=db_path) is None


def test_readonly_connection(tmp_path):
    """The consumer layer must open the database in read-only mode."""
    db_path = _make_fake_db(tmp_path)
    conn = pcc._connect_db(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE probe (x INTEGER)")
    finally:
        conn.close()


# VP-020: schema change detection (column added / removed) must not raise.


def _write_db_with_schema(tmp_path, schema_sql, rows):
    db_path = tmp_path / "cc-switch-schema.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(schema_sql)
        for sql, params in rows:
            conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_column_missing_url_returns_empty(tmp_path):
    """Removed column 'url' is tolerated — endpoint defaults to ""."""
    schema = (
        "CREATE TABLE provider_configs "
        "(id TEXT PRIMARY KEY, model TEXT NOT NULL, extra_params TEXT)"
    )
    rows = [
        (
            "INSERT INTO provider_configs (id, model, extra_params) VALUES (?, ?, ?)",
            ("Vendor B Pro", "b-pro-4", '{"temperature": 0.7}'),
        )
    ]
    db_path = _write_db_with_schema(tmp_path, schema, rows)
    cfg = get_provider("Vendor B Pro", db_path=db_path)
    assert cfg == ProviderConfig(
        name="Vendor B Pro",
        env={"temperature": 0.7},
        base_url="",
        model="b-pro-4",
    )


def test_column_missing_model_returns_none(tmp_path):
    """Removed column 'model' is tolerated — value defaults to None."""
    schema = (
        "CREATE TABLE provider_configs "
        "(id TEXT PRIMARY KEY, url TEXT NOT NULL, extra_params TEXT)"
    )
    rows = [
        (
            "INSERT INTO provider_configs (id, url, extra_params) VALUES (?, ?, ?)",
            ("Vendor B Pro", "https://example.com/v1", "{}"),
        )
    ]
    db_path = _write_db_with_schema(tmp_path, schema, rows)
    cfg = get_provider("Vendor B Pro", db_path=db_path)
    assert cfg == ProviderConfig(
        name="Vendor B Pro",
        env={},
        base_url="https://example.com/v1",
        model=None,
    )


def test_column_missing_extra_params_returns_empty(tmp_path):
    """Removed column 'extra_params' is tolerated — value defaults to empty dict."""
    schema = (
        "CREATE TABLE provider_configs "
        "(id TEXT PRIMARY KEY, url TEXT NOT NULL, model TEXT NOT NULL)"
    )
    rows = [
        (
            "INSERT INTO provider_configs (id, url, model) VALUES (?, ?, ?)",
            ("Vendor B Pro", "https://example.com/v1", "b-pro-4"),
        )
    ]
    db_path = _write_db_with_schema(tmp_path, schema, rows)
    cfg = get_provider("Vendor B Pro", db_path=db_path)
    assert cfg == ProviderConfig(
        name="Vendor B Pro",
        env={},
        base_url="https://example.com/v1",
        model="b-pro-4",
    )


def test_schema_change_added_column_ignored(tmp_path):
    """New column added to the table is ignored — no exception raised."""
    schema = (
        "CREATE TABLE provider_configs "
        "(id TEXT PRIMARY KEY, url TEXT NOT NULL, model TEXT NOT NULL, "
        "extra_params TEXT, api_key TEXT, created_at TEXT)"
    )
    rows = [
        (
            "INSERT INTO provider_configs (id, url, model, extra_params, api_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "Vendor B Pro",
                "https://api.vendor-b.example/v1",
                "b-pro-4",
                '{"temperature": 0.7}',
                "secret-token",
                "2026-06-16T00:00:00Z",
            ),
        )
    ]
    db_path = _write_db_with_schema(tmp_path, schema, rows)
    cfg = get_provider("Vendor B Pro", db_path=db_path)
    assert cfg == ProviderConfig(
        name="Vendor B Pro",
        env={"temperature": 0.7},
        base_url="https://api.vendor-b.example/v1",
        model="b-pro-4",
    )


def test_schema_change_extra_columns_do_not_break_enumeration(tmp_path):
    """Enumeration tolerates extra columns on the legacy table.

    Extra columns must not break the read — and, since the legacy layout
    has no ``name`` column, the result is an empty list (see
    ``test_list_provider_names_legacy_schema``).
    """
    schema = (
        "CREATE TABLE provider_configs "
        "(id TEXT PRIMARY KEY, url TEXT NOT NULL, model TEXT NOT NULL, "
        "extra_params TEXT, notes TEXT)"
    )
    rows = [
        (
            "INSERT INTO provider_configs (id, url, model, extra_params, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "Vendor A Default",
                "https://api.vendor-a.chat/v1",
                "Vendor A-Text-01",
                "{}",
                "test note",
            ),
        ),
        (
            "INSERT INTO provider_configs (id, url, model, extra_params, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "Vendor B Pro",
                "https://api.vendor-b.example/v1",
                "b-pro-4",
                "{}",
                None,
            ),
        ),
    ]
    db_path = _write_db_with_schema(tmp_path, schema, rows)
    assert list_provider_names(db_path=db_path) == []


# ---------------------------------------------------------------------------
# Production CC Switch ``providers`` table schema.
# ---------------------------------------------------------------------------


def _make_production_db(tmp_path, rows):
    """Create a fake cc-switch.db with the production ``providers`` table."""
    db_path = tmp_path / "cc-switch-prod.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE providers (
                id TEXT PRIMARY KEY,
                app_type TEXT NOT NULL,
                name TEXT NOT NULL,
                settings_config TEXT NOT NULL
            )
            """
        )
        for row in rows:
            conn.execute(
                "INSERT INTO providers (id, app_type, name, settings_config) VALUES (?, ?, ?, ?)",
                row,
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _settings(env):
    return json.dumps({"env": env})


def test_get_provider_config_production_schema(tmp_path):
    db_path = _make_production_db(
        tmp_path,
        [
            (
                "a1b2c3d4-0000-4000-8000-000000000001",
                "claude",
                "Vendor B Pro",
                _settings(
                    {
                        "ANTHROPIC_BASE_URL": "https://api.vendor-b.example/anthropic",
                        "ANTHROPIC_AUTH_TOKEN": "vendor-b-token",
                        "ANTHROPIC_MODEL": "b-pro-5.1",
                    }
                ),
            ),
        ],
    )
    cfg = get_provider("Vendor B Pro", db_path=db_path)
    assert cfg == ProviderConfig(
        name="Vendor B Pro",
        env={
            "ANTHROPIC_BASE_URL": "https://api.vendor-b.example/anthropic",
            "ANTHROPIC_AUTH_TOKEN": "vendor-b-token",
            "ANTHROPIC_MODEL": "b-pro-5.1",
        },
        base_url="https://api.vendor-b.example/anthropic",
        model="b-pro-5.1",
    )


def test_get_provider_config_production_by_name(tmp_path):
    db_path = _make_production_db(
        tmp_path,
        [
            (
                "a1b2c3d4-0000-4000-8000-000000000001",
                "claude",
                "Vendor B Pro",
                _settings(
                    {
                        "ANTHROPIC_BASE_URL": "https://api.vendor-b.example/anthropic",
                        "ANTHROPIC_AUTH_TOKEN": "vendor-b-token",
                    }
                ),
            ),
        ],
    )
    cfg = get_provider("Vendor B Pro", db_path=db_path)
    assert cfg.name == "Vendor B Pro"
    assert cfg.base_url == "https://api.vendor-b.example/anthropic"
    assert cfg.api_key == "vendor-b-token"


def test_get_provider_config_production_no_url_skipped(tmp_path):
    db_path = _make_production_db(
        tmp_path,
        [
            (
                "a1b2c3d4-0000-4000-8000-000000000002",
                "claude",
                "Vendor D",
                _settings({"ANTHROPIC_AUTH_TOKEN": "vendor-d-token"}),
            ),
        ],
    )
    assert get_provider("Vendor D", db_path=db_path) is None


def test_list_provider_ids_production_schema(tmp_path):
    db_path = _make_production_db(
        tmp_path,
        [
            (
                "a1b2c3d4-0000-4000-8000-000000000001",
                "claude",
                "Vendor B Pro",
                _settings(
                    {
                        "ANTHROPIC_BASE_URL": "https://api.vendor-b.example/anthropic",
                        "ANTHROPIC_AUTH_TOKEN": "vendor-b-token",
                    }
                ),
            ),
            (
                "a1b2c3d4-0000-4000-8000-000000000003",
                "claude",
                "Vendor C App",
                _settings(
                    {
                        "ANTHROPIC_BASE_URL": "https://api.vendor-c.com/coding/",
                        "ANTHROPIC_AUTH_TOKEN": "vendor-c-token",
                    }
                ),
            ),
            (
                "default",
                "opencode",
                "default",
                _settings({}),
            ),
        ],
    )
    ids = list_provider_names(db_path=db_path)
    assert "Vendor B Pro" in ids
    assert "Vendor C App" in ids
    assert "default" not in ids


def test_get_provider_config_prefer_exact_name_match(tmp_path):
    db_path = _make_production_db(
        tmp_path,
        [
            (
                "a1b2c3d4-0000-4000-8000-000000000004",
                "claude",
                "Vendor A",
                _settings(
                    {
                        "ANTHROPIC_BASE_URL": "https://api.vendor-a.example/anthropic",
                        "ANTHROPIC_AUTH_TOKEN": "vendor-a-generic-token",
                    }
                ),
            ),
            (
                "a1b2c3d4-0000-4000-8000-000000000005",
                "claude",
                "Vendor A Pro",
                _settings(
                    {
                        "ANTHROPIC_BASE_URL": "https://api.vendor-a.example/anthropic",
                        "ANTHROPIC_AUTH_TOKEN": "vendor-a-pro-token",
                    }
                ),
            ),
        ],
    )
    cfg = get_provider("Vendor A Pro", db_path=db_path)
    assert cfg.api_key == "vendor-a-pro-token"


def test_provider_configs_takes_precedence_over_providers(tmp_path):
    """When both tables exist, the legacy provider_configs table wins."""
    db_path = tmp_path / "cc-switch-both.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE provider_configs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                model TEXT NOT NULL,
                extra_params TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO provider_configs (id, url, model, extra_params) VALUES (?, ?, ?, ?)",
            ("Vendor B Pro", "https://legacy.example.com", "b-pro-4", "{}"),
        )
        conn.execute(
            """
            CREATE TABLE providers (
                id TEXT PRIMARY KEY,
                app_type TEXT NOT NULL,
                name TEXT NOT NULL,
                settings_config TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO providers (id, app_type, name, settings_config) VALUES (?, ?, ?, ?)",
            (
                "a1b2c3d4-0000-4000-8000-000000000001",
                "claude",
                "Vendor B Pro",
                _settings(
                    {
                        "ANTHROPIC_BASE_URL": "https://production.example.com",
                        "ANTHROPIC_AUTH_TOKEN": "prod-token",
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    cfg = get_provider("Vendor B Pro", db_path=db_path)
    assert cfg.base_url == "https://legacy.example.com"


# ---------------------------------------------------------------------------
# The value shape: one normalized object for both on-disk layouts.
#
# Until 2026-09-25 ``get_provider`` handed back a dict keyed on the *legacy*
# table's column names, and each consumer re-derived the endpoint, the
# credential and the model tiers from it. That freedom is exactly how three
# parsers which disagreed about where the API key lives came to exist, so
# the projection is now pinned here rather than left to each caller.
# ---------------------------------------------------------------------------


def _legacy_db_with_env(tmp_path, name, env, *, url=None, model=None):
    """A legacy ``provider_configs`` row carrying *env* as extra_params."""
    db_path = tmp_path / "cc-switch-legacy.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE provider_configs ("
            "id TEXT PRIMARY KEY, url TEXT, model TEXT, extra_params TEXT)"
        )
        conn.execute(
            "INSERT INTO provider_configs (id, url, model, extra_params) "
            "VALUES (?, ?, ?, ?)",
            (name, url, model, json.dumps(env)),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_both_layouts_project_the_same_provider_identically(tmp_path):
    """The point of the normalization: the table that answered is invisible.

    A modern row keeps the endpoint and the model tiers inside its
    ``settings_config.env``; a legacy row keeps the endpoint in a column
    and the tiers in ``extra_params``. A consumer must not have to know
    which of the two it is talking to, so both are asked for the same
    provider and the answers are compared field by field.
    """
    env = {
        "ANTHROPIC_BASE_URL": "https://shared.example.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "shared-token",
        "ANTHROPIC_MODEL": "shared-default",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "shared-opus",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "shared-sonnet",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "shared-haiku",
    }
    modern = get_provider(
        "Shared Provider",
        db_path=_make_production_db(
            tmp_path, [("id-1", "claude", "Shared Provider", _settings(env))]
        ),
    )
    legacy = get_provider(
        "Shared Provider",
        db_path=_legacy_db_with_env(
            tmp_path, "Shared Provider", env, url=env["ANTHROPIC_BASE_URL"],
            model=env["ANTHROPIC_MODEL"],
        ),
    )

    assert modern is not None and legacy is not None
    assert modern.base_url == legacy.base_url == "https://shared.example.com/anthropic"
    assert modern.api_key == legacy.api_key == "shared-token"
    assert modern.model == legacy.model == "shared-default"
    assert modern.models == legacy.models == {
        "default": "shared-default",
        "opus": "shared-opus",
        "sonnet": "shared-sonnet",
        "haiku": "shared-haiku",
    }
    assert modern.is_dispatchable() and legacy.is_dispatchable()


def test_api_key_accepts_either_spelling_preferring_the_auth_token():
    """Which of the two credential keys a row used is not the caller's problem.

    CC Switch writes ``ANTHROPIC_AUTH_TOKEN``; rows exist in the wild
    carrying ``ANTHROPIC_API_KEY`` instead. Both are the same credential,
    and a consumer that reads only one of them silently sees "no token".
    """
    def cfg(env):
        return ProviderConfig(name="P", env=env)

    assert cfg({"ANTHROPIC_AUTH_TOKEN": "a"}).api_key == "a"
    assert cfg({"ANTHROPIC_API_KEY": "b"}).api_key == "b"
    assert cfg({"ANTHROPIC_AUTH_TOKEN": "a", "ANTHROPIC_API_KEY": "b"}).api_key == "a"
    assert cfg({}).api_key == ""


def test_an_endpoint_without_a_credential_is_not_dispatchable(tmp_path):
    """The "added but never signed in" row is returned, and says so.

    ``get_provider`` answers "is there a row named X with an endpoint?"
    and that answer is yes here — the row is real and a caller may want
    to report it. What must not happen is a dispatch to it, so the
    distinction lives on the config (``is_dispatchable``) rather than
    being folded into "the lookup found nothing", which would make an
    unconfigured provider indistinguishable from a misspelled one.
    """
    cfg = get_provider(
        "Half Configured",
        db_path=_make_production_db(
            tmp_path,
            [
                (
                    "id-1",
                    "claude",
                    "Half Configured",
                    _settings({"ANTHROPIC_BASE_URL": "https://half.example.com"}),
                )
            ],
        ),
    )
    assert cfg is not None
    assert cfg.base_url == "https://half.example.com"
    assert cfg.api_key == ""
    assert cfg.is_dispatchable() is False


def test_current_provider_is_none_not_an_empty_dict_when_absent(
    tmp_path, monkeypatch
):
    """The "no current provider" answer is ``None``, and it stays falsy.

    Callers branch with ``if cc_provider:``, so the absent case has to be
    falsy; ``None`` says that in the type as well as at runtime, whereas
    the ``{}`` this used to return left the type claiming a
    ``ProviderConfig`` was always present.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert current_provider(db_path=tmp_path / "nope.db") is None
    assert not current_provider(db_path=tmp_path / "nope.db")


def test_current_provider_agrees_with_the_by_name_reader(tmp_path, monkeypatch):
    """Both readers project the same row the same way.

    ``current_provider`` resolves ``settings.json`` → a row id, while
    ``get_provider`` resolves a name → a row. They are different lookups
    into the same table, so for the same row they must produce the same
    endpoint, credential and tiers — otherwise the scene-less dispatch
    and the named dispatch disagree about one provider.
    """
    home = tmp_path / "home"
    (home / ".cc-switch").mkdir(parents=True, exist_ok=True)
    (home / ".cc-switch" / "settings.json").write_text(
        json.dumps({"currentProviderClaude": "id-current"}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))

    env = {
        "ANTHROPIC_BASE_URL": "https://current.example.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "current-token",
        "ANTHROPIC_MODEL": "current-default",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "current-opus",
    }
    db_path = _make_production_db(
        tmp_path, [("id-current", "claude", "Current Provider", _settings(env))]
    )

    by_name = get_provider("Current Provider", db_path=db_path)
    current = current_provider(db_path=db_path)

    assert by_name is not None and current is not None
    assert current.name == "Current Provider"
    assert current.base_url == by_name.base_url
    assert current.api_key == by_name.api_key
    assert current.models == by_name.models


def test_opening_a_database_is_validated_by_reading_the_schema(monkeypatch, tmp_path):
    """The validation query must be one that actually reads the file.

    ``test_invalid_database_file_raises`` above is the behavioural
    contract, but it cannot be the only pin: whether ``SELECT 1`` happens
    to touch the database header is an incidental property of the sqlite
    build, and it differs between platforms. On the Python this repository
    developed on, ``SELECT 1`` raised ``DatabaseError`` on a text file, so
    the old probe appeared to work; on the CI runner's it did not — the
    text file sailed through validation and a caller's own query raised a
    raw ``sqlite3.DatabaseError`` from the middle of a lookup. A test that
    passes on one platform and fails on another is not pinning anything.

    So this asserts the mechanism instead: a statement that names no table
    cannot be the validation, because SQLite can answer it without ever
    reading the file. ``sqlite_master`` is the one table it must parse
    before it can answer anything.

    The recording is done with a proxy rather than by patching
    ``sqlite3.Connection.execute``: that attribute lives on an immutable C
    type and cannot be set.
    """
    bad = tmp_path / "not-a-db.db"
    bad.write_text("this is not sqlite")
    executed: list[str] = []

    class _RecordingConnection:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            executed.append(sql)
            return self._real.execute(sql, *args, **kwargs)

        def close(self):
            self._real.close()

    real_connect = sqlite3.connect

    def _recording_connect(*args, **kwargs):
        return _RecordingConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(pcc.sqlite3, "connect", _recording_connect)

    with pytest.raises(CCSwitchError):
        pcc._connect_db(bad)

    assert executed, "the opener ran no statement at all, so it validated nothing"
    assert any("sqlite_master" in sql for sql in executed), (
        f"the validation query never reads the schema ({executed!r}); a "
        f"statement that names no table cannot detect a non-database file"
    )
