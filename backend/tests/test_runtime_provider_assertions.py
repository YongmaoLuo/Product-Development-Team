"""Runtime assertion tests: every provider that reaches the dispatch
layer must name a live row in the CC Switch database.

This is the CI gate for the single-source-of-truth contract that
replaced VP-010. The deleted invariant was "every provider ID conforms
to a kebab-case regex" — an assertion about a *local* vocabulary this
project invented. Providers no longer have a second name: the string
CC Switch shows (``providers.name``) is the string
``provider_routing.yaml`` matches its regexes against, the string the
chain carries, and the key every lookup uses. So "is it kebab-case?"
is not a question the system asks any more, and the invariant that
actually protects the dispatch is:

    **every provider name CC Switch reports can be looked up by exactly
    that name, and anything that cannot be resolved is dropped before it
    reaches a dispatch.**

Critical paths covered
----------------------

1. ``cc_switch.get_provider`` — the name
   is used verbatim: no case folding, no separator rewriting, no fuzzy
   match. An unknown name is ``None``; a malformed one is
   ``CCSwitchError``.
2. ``cc_switch.list_provider_names`` — the
   enumeration is the DB's own roster, and nothing outside it.
3. ``provider_order.load_fallback_order`` — the returned chain is what
   CC Switch reported, in CC Switch's order. It applies no membership
   filter of its own: filtering would make the chain a function of the
   database as well as of CC Switch, which is how a chain can disagree
   with the application the operator is looking at.
4. ``server.load_providers`` — where the names are resolved, and where
   one that does not resolve is dropped with a warning. That is the
   layer that has the information to make the decision.
5. Cross-cutting: everything that survives into the dispatch chain
   round-trips back through the by-name lookup. That is the property an
   operator depends on — a chain entry that cannot be resolved is a
   sub-agent dispatched with whatever the parent env happened to say.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import List
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Path setup -- mirror the project layout used by sibling test files.
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# Import the modules under test AFTER sys.path is set up.
import cc_switch  # noqa: E402
import provider_order  # noqa: E402
from cc_switch import (  # noqa: E402
    CCSwitchError,
    get_provider,
    list_provider_names,
)
from provider_order import ProviderOrderError  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _settings(env: dict) -> str:
    return json.dumps({"env": env})


def _make_fake_cc_switch_db(db_path: Path, provider_names: List[str]) -> Path:
    """Create a fake CC Switch DB at ``db_path`` declaring *provider_names*.

    Production ``providers`` schema: the row's ``name`` is the provider's
    identity, and the endpoint + credential live in
    ``settings_config.env``. ``app_type`` and the ordering columns are
    here because the chain reader filters on the app type and sorts by
    them.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE providers ("
            "id TEXT NOT NULL, app_type TEXT NOT NULL, name TEXT NOT NULL, "
            "settings_config TEXT NOT NULL, created_at INTEGER, "
            "sort_index INTEGER, is_current BOOLEAN NOT NULL DEFAULT 0, "
            "PRIMARY KEY (id, app_type))"
        )
        for i, name in enumerate(provider_names):
            conn.execute(
                "INSERT INTO providers (id, app_type, name, settings_config, "
                "created_at, sort_index) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"row-{i}",
                    "claude",
                    name,
                    _settings({
                        "ANTHROPIC_BASE_URL": f"https://example.test/{i}/v1",
                        "ANTHROPIC_AUTH_TOKEN": f"sk-{i}",
                    }),
                    i,
                    i,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _install_fake_home_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_names: List[str],
) -> Path:
    """Install a fake ``~/.cc-switch/cc-switch.db`` under a fake ``$HOME``."""
    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path = cc_dir / "cc-switch.db"
    _make_fake_cc_switch_db(db_path, provider_names)
    monkeypatch.setenv("HOME", str(tmp_path))
    return db_path


#: An authority nothing can be listening on, so the HTTP leg fails the
#: way it does when CC Switch is closed and the database has to answer.
API_DOWN = "http://127.0.0.1:1"


def _serve_cc_switch(monkeypatch: pytest.MonkeyPatch, order: List[str]) -> None:
    """Make CC Switch's HTTP provider list report ``order``."""
    body = json.dumps(
        {"providers": [{"id": f"id-{i}", "name": n} for i, n in enumerate(order)]}
    ).encode()

    class _Response:
        status = 200

        def read(self):
            return body

        def getcode(self):
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        provider_order.urllib.request, "urlopen", lambda *a, **k: _Response()
    )


# ---------------------------------------------------------------------------
# 1. The named lookup is exact
# ---------------------------------------------------------------------------


def test_a_name_resolves_verbatim(tmp_path):
    """A row is found by exactly the string CC Switch gave it."""
    db_path = _make_fake_cc_switch_db(
        tmp_path / "db.sqlite", ["Vendor B Pro", "Vendor A Pro"]
    )
    cfg = get_provider("Vendor B Pro", db_path=db_path)
    assert cfg is not None
    assert cfg.name == "Vendor B Pro"
    assert cfg.base_url == "https://example.test/0/v1"


@pytest.mark.parametrize(
    "near_miss",
    [
        "vendor-b b-pro",        # lowercased
        "VENDOR B PRO",        # uppercased
        "Vendor B  B-PRO",       # doubled space
        " Vendor B Pro",       # leading space
        "Vendor B Pro ",       # trailing space
        "Vendor-B-Pro",        # hyphen for space
        "vendor-b-pro",        # the deleted kebab id
        "Vendor B Pro API",    # prefix of a different provider
        "Vendor B",            # prefix
        "B-PRO",              # suffix
    ],
)
def test_a_near_miss_name_does_not_resolve(near_miss, tmp_path):
    """Nothing normalizes, tokenizes or fuzzy-matches a provider name.

    Each entry here is a plausible "helpful" transformation of the real
    row name ``"Vendor B Pro"``. If any of them resolved, the system would
    be quietly running against a provider the operator never selected —
    and the log would name the one they did select.
    """
    db_path = _make_fake_cc_switch_db(tmp_path / "db.sqlite", ["Vendor B Pro"])
    assert get_provider(near_miss, db_path=db_path) is None


@pytest.mark.parametrize("bad_name", [123, None, {"name": "Vendor B Pro"}, ["Vendor B Pro"], ""])
def test_a_malformed_name_is_an_error_not_a_lookup(bad_name, tmp_path):
    """Non-string / empty names are a programming error, not a miss."""
    db_path = _make_fake_cc_switch_db(tmp_path / "db.sqlite", ["Vendor B Pro"])
    with pytest.raises(CCSwitchError):
        get_provider(bad_name, db_path=db_path)


def test_enumeration_is_the_databases_own_roster(tmp_path):
    """``list_provider_names`` returns exactly what the DB declares.

    The deleted ``_CC_SWITCH_NAME_TO_ID`` table was a hard-coded
    whitelist, and the live regression was that it went stale: CC Switch
    really had six providers while ID-based enumeration reported three,
    because the three newer rows were spelled in a way the table had
    never heard of. The roster must come from the database.
    """
    declared = ["Vendor B Pro", "Vendor A Pro", "Vendor C App",
                "Vendor D API", "Vendor B Pro API", "Vendor A API"]
    db_path = _make_fake_cc_switch_db(tmp_path / "db.sqlite", declared)
    assert sorted(list_provider_names(db_path=db_path)) == sorted(declared)


def test_enumeration_skips_rows_that_cannot_drive_a_subagent(tmp_path):
    """A row with no endpoint is not dispatchable, so it is not offered."""
    conn = sqlite3.connect(str(tmp_path / "db.sqlite"))
    try:
        conn.execute(
            "CREATE TABLE providers ("
            "id TEXT PRIMARY KEY, name TEXT, settings_config TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO providers (id, name, settings_config) VALUES (?, ?, ?)",
            ("row-0", "Vendor B Pro", _settings({"ANTHROPIC_BASE_URL": "https://z/v1",
                                              "ANTHROPIC_AUTH_TOKEN": "k"})),
        )
        conn.execute(
            "INSERT INTO providers (id, name, settings_config) VALUES (?, ?, ?)",
            ("row-1", "No Endpoint", _settings({"ANTHROPIC_AUTH_TOKEN": "k"})),
        )
        conn.commit()
    finally:
        conn.close()

    names = list_provider_names(db_path=tmp_path / "db.sqlite")
    assert "Vendor B Pro" in names
    assert "No Endpoint" not in names


# ---------------------------------------------------------------------------
# 2. The chain is CC Switch's roster, and the lookup can answer for it
# ---------------------------------------------------------------------------


def test_the_chain_is_the_cc_switch_roster_verbatim(
    tmp_path, monkeypatch, real_cc_switch
):
    """The reader applies no membership filter.

    A name CC Switch reported is in the chain whether or not a row
    resolves it. Filtering here would make the chain a function of the
    database as well as of CC Switch — and would drop precisely the
    newest row, the one CC Switch has and this backend has not learned
    to parse yet. That is a transient state, not a misconfiguration,
    and hiding it would make an operator's freshly-added provider look
    as though it had never been added.
    """
    declared = ["Vendor A Pro", "Vendor B Pro"]
    _install_fake_home_db(tmp_path, monkeypatch, declared)

    order = ["Vendor A Pro", "Ghost Provider", "Vendor B Pro", "parent"]
    _serve_cc_switch(monkeypatch, order)

    provider_order.cache_clear()
    chain = provider_order.load_fallback_order()

    assert chain == order, (
        f"load_fallback_order returned {chain!r}; expected CC Switch's "
        f"own list {order!r}, unfiltered and in order"
    )


def test_every_declared_chain_entry_round_trips_through_the_lookup(
    tmp_path, monkeypatch, real_cc_switch
):
    """The property the dispatch depends on: declared names are resolvable.

    This is the assertion that subsumes "is the name well-formed?" — a
    name is usable exactly when looking it up returns a row. An entry
    that fails this is a sub-agent dispatched against the parent env
    while the logs name somebody else.
    """
    declared = ["Vendor B Pro", "Vendor A Pro", "Vendor C App"]
    db_path = _install_fake_home_db(tmp_path, monkeypatch, declared)

    _serve_cc_switch(monkeypatch, declared)
    provider_order.cache_clear()
    chain = provider_order.load_fallback_order()

    assert chain == declared
    for entry in chain:
        cfg = get_provider(entry, db_path=db_path)
        assert cfg is not None, (
            f"chain entry {entry!r} has no CC Switch row — the dispatch "
            "would fall back to the parent environment while the log "
            "claimed this provider served the call"
        )
        assert cfg.base_url, f"chain entry {entry!r} resolved to no endpoint"


def test_load_fallback_order_raises_when_neither_cc_switch_surface_answers(
    tmp_path, monkeypatch, real_cc_switch
):
    """No CC Switch at all ⇒ a hard error, not a silent empty chain.

    An empty chain and an unresolvable one mean opposite things: the
    first is "CC Switch lists nothing usable", the second is "this
    process cannot see CC Switch at all". Collapsing them would let a
    broken deployment look like a deliberate configuration.
    """
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.cc-switch/cc-switch.db
    monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", "http://127.0.0.1:1")

    provider_order.cache_clear()
    with pytest.raises(ProviderOrderError):
        provider_order.load_fallback_order()


# ---------------------------------------------------------------------------
# 3. Cross-cutting: a bogus name cannot reach the dispatch layer
# ---------------------------------------------------------------------------


def test_a_bogus_name_cannot_reach_the_dispatch_chain(
    tmp_path, monkeypatch, real_cc_switch
):
    """End to end: an unknown name is refused at every layer it can be.

    It survives the chain reader — that is CC Switch's list, not an
    opinion about it — and is dropped by ``server.load_providers``,
    which is the layer that can tell whether a name resolves. The
    dispatch never sees it either way.
    """
    declared = ["Vendor B Pro", "Vendor A Pro"]
    db_path = _install_fake_home_db(tmp_path, monkeypatch, declared)

    bogus = "Totally Unknown Vendor"

    # 1. The lookup refuses to invent a config for it.
    assert get_provider(bogus, db_path=db_path) is None

    # 2. Enumeration does not offer it.
    assert bogus not in list_provider_names(db_path=db_path)

    # 3. The chain carries it — CC Switch listed it — and the startup
    #    resolver drops it, naming it in a warning.
    import logging

    import server

    _serve_cc_switch(monkeypatch, [bogus, *declared])
    provider_order.cache_clear()
    caplog_records: List[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            caplog_records.append(record.getMessage())

    handler = _Capture()
    logging.getLogger("server").addHandler(handler)
    try:
        providers = server.load_providers()
    finally:
        logging.getLogger("server").removeHandler(handler)

    resolved = [cfg.name for cfg in providers]
    assert bogus not in resolved, (
        f"an unknown provider {bogus!r} reached the dispatch chain "
        f"{resolved!r}"
    )
    assert resolved == declared
    assert any(bogus in m for m in caplog_records), (
        f"the skip must name what was skipped; got {caplog_records!r}"
    )
