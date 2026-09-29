"""One database path, every reader.

Before this was centralised (2026-09-24), five places decided where CC
Switch's database lives and only one of them honoured an override:

===============================  ==========================================
``cc_switch.resolve_db_path``    ``PDT_CC_SWITCH_DB`` → candidates → default
``cc_switch`` (ex-``provider_config_consumer``)
                                  hard-coded ``~/.cc-switch/cc-switch.db``
``coding_tool`` (two sites)       hard-coded ``~/.cc-switch/cc-switch.db``
``plan_usage``                    ``CC_SWITCH_DB`` → hard-coded default
===============================  ==========================================

So an operator who relocated their install — or a test that redirected
one reader — got a system where ``probe()`` reported *one*
file as available while the reader opened *another*. The probe was not
lying about a file it had checked; it was answering about a file nobody
else used.

The contract these tests pin is not "there is a helper" but **"the
readers agree"**. Each test writes a database at a distinctive path and
then asks every reader where it looked; a reader that still joins
``~/.cc-switch`` by hand fails here even though it would pass a test
that only exercised that reader alone.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import cc_switch  # noqa: E402
from coding_tool import ClaudeCodingTool  # noqa: E402


PROVIDER_NAME = "Convergence Probe Provider"


def _settings_config(base_url: str) -> str:
    return json.dumps({
        "env": {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_AUTH_TOKEN": "sk-convergence",
            "ANTHROPIC_MODEL": "model-from-the-override",
        }
    })


@pytest.fixture(autouse=True)
def hermetic_cc_switch_current_provider():
    """Do **not** stub ``_load_cc_switch_current_provider`` in this module.

    ``tests/conftest.py`` defines an autouse fixture of this exact name
    that replaces the method with ``lambda: {}`` suite-wide, so that
    provider-selection tests never read the operator's live CC Switch.
    That is right for the suite and wrong for this module, whose entire
    subject is where the readers *look* — the stub would answer ``{}``
    without touching a database and every assertion here would pass
    vacuously.

    Defining a fixture of the same name at module scope overrides the
    conftest one for this file only, leaving it in force everywhere else.

    The protection the stub provides is not lost: every test in this
    module redirects ``$HOME`` to ``tmp_path``, so the real resolver
    cannot reach the operator's ``~/.cc-switch`` even unstubbed.
    """
    yield


def _write_db(db_path: Path, base_url: str, *, extra_rows=()) -> Path:
    """Write a CC Switch DB carrying one named provider.

    The schema is the full production one, not the minimum a particular
    reader needs. ``cc_switch.probe`` deliberately requires
    ``app_type`` and ``is_current`` as well — it fails a database that
    would break a *later* dispatch, which is a much worse place to
    discover a missing column — so a fixture with only the three columns
    the by-name reader touches would test the wrong database. ``rows``
    lets a test add a second provider so the ``currentProviderClaude``
    pointer has somewhere to point that is not the ``is_current`` row.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE providers (
                id TEXT NOT NULL,
                app_type TEXT NOT NULL,
                name TEXT NOT NULL,
                settings_config TEXT NOT NULL,
                is_current BOOLEAN NOT NULL DEFAULT 0,
                PRIMARY KEY (id, app_type)
            )
            """
        )
        rows = [
            ("row-0", "claude", PROVIDER_NAME, _settings_config(base_url), 1),
            *extra_rows,
        ]
        conn.executemany(
            "INSERT INTO providers "
            "(id, app_type, name, settings_config, is_current) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture
def relocated_db(tmp_path, monkeypatch):
    """A CC Switch database at a non-default path, named the modern way.

    ``HOME`` is redirected too, so the *default* location the readers
    used to join by hand does not exist at all — a reader that ignores
    the override cannot accidentally succeed by finding a real install.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CC_SWITCH_DB", raising=False)

    db_path = _write_db(
        tmp_path / "elsewhere" / "cc-switch.db",
        "https://relocated.example.com/anthropic",
    )
    monkeypatch.setenv("PDT_CC_SWITCH_DB", str(db_path))
    return db_path


def test_the_probe_reports_the_overridden_database(relocated_db):
    status = cc_switch.probe()
    assert status.available is True
    assert status.path == relocated_db


def test_the_provider_reader_opens_the_overridden_database(relocated_db):
    cfg = cc_switch.get_provider(PROVIDER_NAME)
    assert cfg is not None, (
        "the provider reader did not find the row that exists in the "
        "database PDT_CC_SWITCH_DB names — it is reading somewhere else"
    )
    assert cfg.base_url == "https://relocated.example.com/anthropic"


def test_the_roster_reader_opens_the_overridden_database(relocated_db):
    assert PROVIDER_NAME in cc_switch.list_provider_names()


def test_coding_tool_opens_the_overridden_database(relocated_db):
    cfg = ClaudeCodingTool._load_provider_from_cc_switch_db(PROVIDER_NAME)
    assert cfg.get("base_url") == "https://relocated.example.com/anthropic", (
        f"coding_tool resolved {cfg!r} instead of the overridden database's row"
    )


def test_coding_tool_falls_back_to_the_is_current_row(relocated_db):
    """With no ``settings.json`` the overridden database answers anyway.

    ``current_provider`` resolves ``settings.json::currentProviderClaude``
    against the database, and falls back to the row flagged
    ``is_current``. The fallback must read the *overridden* database —
    reaching for a default install that is not there would return ``{}``
    and silently drop the dispatch to the parent environment.
    """
    current = ClaudeCodingTool._load_cc_switch_current_provider()
    assert current is not None
    assert current.base_url == "https://relocated.example.com/anthropic"


def test_coding_tool_follows_the_settings_pointer(tmp_path, monkeypatch):
    """``settings.json`` names the provider, and it beats ``is_current``.

    The two files are read as a pair, so both have to be resolved by the
    same module: the pointer lives in ``settings.json`` while the
    endpoint it names lives in the database, and a reader that resolves
    them from different places cannot find its own row.
    """
    home = tmp_path / "home"
    (home / ".cc-switch").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CC_SWITCH_DB", raising=False)

    db_path = _write_db(
        tmp_path / "elsewhere" / "cc-switch.db",
        "https://is-current.example.com/anthropic",
        extra_rows=[
            (
                "pointed-at",
                "claude",
                "Pointed At Provider",
                _settings_config("https://pointed-at.example.com/anthropic"),
                0,
            )
        ],
    )
    monkeypatch.setenv("PDT_CC_SWITCH_DB", str(db_path))
    (home / ".cc-switch" / "settings.json").write_text(
        json.dumps({"currentProviderClaude": "pointed-at"}), encoding="utf-8"
    )

    current = ClaudeCodingTool._load_cc_switch_current_provider()
    assert current is not None, "the settings.json pointer was ignored"
    assert current.base_url == "https://pointed-at.example.com/anthropic", (
        f"the settings.json pointer was ignored: got {current!r}"
    )
    assert current.name == "Pointed At Provider"


def test_every_reader_sees_the_overridden_database(relocated_db):
    """The whole point, asserted in one place.

    Every reader is asked a question whose answer exists **only** in the
    overridden database, and is observed *through behaviour* rather than
    by asking a shared resolver where it points. That distinction is what
    gives the test teeth: a reader that rejoins ``~/.cc-switch`` by hand
    still returns a plausible-looking empty result, and only a question
    about data it cannot see exposes it.

    The five calls below are the CC Switch entry points the dispatch and
    the usage ledger actually use. ``snapshot`` is the one
    :mod:`plan_usage` reads the call ledger through.
    """
    assert cc_switch.probe().path == relocated_db, "the probe looked elsewhere"
    assert cc_switch.get_provider(PROVIDER_NAME) is not None, (
        "the by-name reader did not find the row that exists only in the "
        "overridden database"
    )
    assert PROVIDER_NAME in cc_switch.list_provider_names(), (
        "the roster reader did not see the overridden database"
    )
    assert ClaudeCodingTool._load_provider_from_cc_switch_db(
        PROVIDER_NAME
    ).get("base_url") == "https://relocated.example.com/anthropic"
    _current = ClaudeCodingTool._load_cc_switch_current_provider()
    assert _current is not None and (
        _current.base_url == "https://relocated.example.com/anthropic"
    )

    with cc_switch.snapshot() as copy:
        conn = sqlite3.connect(str(copy))
        try:
            rows = conn.execute("SELECT name FROM providers").fetchall()
        finally:
            conn.close()
    assert [row[0] for row in rows] == [PROVIDER_NAME], (
        "the ledger snapshot copied a database without the overridden "
        f"provider, so plan_usage would read the wrong one: {rows!r}"
    )


def test_the_legacy_cc_switch_db_variable_still_works(tmp_path, monkeypatch):
    """``CC_SWITCH_DB`` keeps meaning what it meant before.

    :mod:`plan_usage` read that name before :mod:`cc_switch` became the
    single resolver. An operator who already exports it must keep getting
    the database they named — so it is honoured, and honoured by *every*
    reader rather than by the one that used to know about it.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PDT_CC_SWITCH_DB", raising=False)

    db_path = _write_db(
        tmp_path / "legacy" / "cc-switch.db",
        "https://legacy-var.example.com/anthropic",
    )
    monkeypatch.setenv("CC_SWITCH_DB", str(db_path))

    assert cc_switch.probe().path == db_path
    assert cc_switch.resolve_db_path() == db_path
    cfg = cc_switch.get_provider(PROVIDER_NAME)
    assert cfg is not None and cfg.base_url == "https://legacy-var.example.com/anthropic"
    with cc_switch.snapshot() as copy:
        assert copy.exists(), "the ledger snapshot did not follow CC_SWITCH_DB"


def test_the_modern_variable_wins_over_the_legacy_one(tmp_path, monkeypatch):
    """When both are exported, ``PDT_CC_SWITCH_DB`` decides.

    The legacy name is a compatibility alias, not an equal partner: it
    exists so an old export keeps working, not so it can override an
    explicit current one.
    """
    modern = _write_db(
        tmp_path / "modern" / "cc-switch.db", "https://modern.example.com"
    )
    legacy = _write_db(
        tmp_path / "legacy" / "cc-switch.db", "https://legacy.example.com"
    )
    monkeypatch.setenv("PDT_CC_SWITCH_DB", str(modern))
    monkeypatch.setenv("CC_SWITCH_DB", str(legacy))

    assert cc_switch.resolve_db_path() == modern


def test_no_variable_means_the_documented_location(tmp_path, monkeypatch):
    """With nothing exported the default is used, and only the default.

    This is the pre-existing behaviour, kept so the convergence cannot be
    mistaken for "the override is now mandatory".
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PDT_CC_SWITCH_DB", raising=False)
    monkeypatch.delenv("CC_SWITCH_DB", raising=False)

    assert cc_switch.resolve_db_path() == home / ".cc-switch" / "cc-switch.db"


def test_a_named_but_missing_override_is_authoritative(tmp_path, monkeypatch):
    """An override that names nothing usable is reported, not bypassed.

    Falling through to the usual locations would mean the system reads a
    database the operator did not name, while every log line names the
    one they did — the substitution is invisible precisely because it
    looks like it worked.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    missing = tmp_path / "typo" / "cc-switch.db"
    monkeypatch.setenv("PDT_CC_SWITCH_DB", str(missing))

    status = cc_switch.probe()
    assert status.available is False
    assert status.path == missing
    assert str(missing) in status.detail


def test_the_settings_file_is_not_redirected_by_the_database_override(
    tmp_path, monkeypatch
):
    """``PDT_CC_SWITCH_DB`` names a *file*, not a directory.

    ``settings.json`` is a separate file with a separate fixed location.
    Letting the database override relocate it too would mean an override
    aimed at one file silently moves another — and the relocation would
    be invisible, because ``current_provider`` treats a missing settings
    file as "fall back to the ``is_current`` row".
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(
        "PDT_CC_SWITCH_DB", str(tmp_path / "elsewhere" / "cc-switch.db")
    )

    assert cc_switch.resolve_settings_path() == (
        tmp_path / "home" / ".cc-switch" / "settings.json"
    )
