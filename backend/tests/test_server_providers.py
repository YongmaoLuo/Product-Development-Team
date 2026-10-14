"""TDD spec for ``backend/server.py`` — provider startup goes through
the consumer layer.

The three pinned contracts (Task 7 — refactor ``server.py`` so all
provider sources flow through the consumption layer + ``provider_order``):

  1. ``test_server_no_hardcoded_providers`` — static AST scan of
     ``server.py`` reveals no list/tuple/dict literal whose value is
     the bare string ``"vendor-b"`` or ``"vendor-a"``.  Bare legacy IDs
     (the form that was hard-coded in
     ``_PROVIDER_CONCURRENCY_CAPS = {"vendor-a": 5, "vendor-b": 3}``) MUST
     not appear as code-level values anywhere in the module — only
     kebab-case IDs (``"vendor-a-pro"`` / ``"vendor-b-pro"``) are
     permitted in code, and even those only when populated from the
     consumer layer at runtime.
  2. ``test_server_startup_reads_db`` — :func:`server.load_providers`
     resolves the chain from a fake CC Switch database and stores the
     resulting configs in :data:`server.STARTUP_PROVIDERS`.  Unknown
     IDs are skipped with a warning, not promoted to a startup
     failure.
  3. ``test_server_startup_no_db_fails`` — when the CC Switch database
     is missing/unreadable, :func:`server.load_providers` raises
     :class:`cc_switch.CCSwitchError` so startup
     fails explicitly rather than silently degrading to a server with
     no provider knowledge.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Set
from unittest.mock import patch

import pytest

import server
from cc_switch import CCSwitchError, ProviderConfig
from provider_order import (
    ProviderOrderError,
    cache_clear as provider_order_cache_clear,
)


SERVER_PY = Path(server.__file__).resolve()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Current local time as a valid ISO 8601 string with offset."""
    return datetime.now(timezone.utc).astimezone().isoformat()


def _make_fake_cc_switch_db(
    tmp_path: Path,
    provider_ids: List[str],
    extra_params: dict | None = None,
) -> Path:
    """Create a fake ``~/.cc-switch/cc-switch.db`` with *provider_ids*."""
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
                "INSERT INTO provider_configs "
                "(id, url, model, extra_params) VALUES (?, ?, ?, ?)",
                (
                    pid,
                    f"https://example.com/{pid}/v1",
                    f"fake-model-{pid}",
                    json.dumps(extra_params or {}),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _install_fake_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_ids: List[str],
) -> Path:
    """Install a fake CC Switch DB under ``$HOME/.cc-switch/cc-switch.db``."""
    db_path = _make_fake_cc_switch_db(tmp_path, provider_ids)
    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path.replace(cc_dir / "cc-switch.db")
    monkeypatch.setenv("HOME", str(tmp_path))
    return cc_dir / "cc-switch.db"


def _make_fake_cc_switch_db_prod(
    tmp_path: Path,
    display_names: List[str],
) -> Path:
    """Create a fake CC Switch DB with the production ``providers`` schema.

    Post-5561e2c the optimizer chain carries CC Switch display names
    and ``server.load_providers`` resolves them via
    ``get_provider``, which queries the production
    ``providers`` table (``id`` / ``name`` / ``settings_config``). The
    legacy ``provider_configs`` fixture schema has no ``name`` column
    and therefore cannot exercise the by-name lookup path.

    The chain filter matches an order entry against the ``name`` column
    verbatim, so the mapping below fixes a stable ``id`` per display
    name rather than letting one be derived from it. A name absent from
    the mapping still gets a row, keyed by the name itself when that is
    already kebab-case and by ``row-{i}`` otherwise.
    """
    name_to_id = {
        "Vendor A Pro": "vendor-a-pro",
        "Vendor A": "vendor-a-pro",
        "Vendor B Pro": "vendor-b-pro",
        "Vendor C App": "vendor-c-app",
        "Vendor D": "vendor-d",
    }
    db_path = tmp_path / "cc-switch-prod.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE providers ("
            "id TEXT NOT NULL, app_type TEXT NOT NULL, name TEXT NOT NULL, "
            "settings_config TEXT NOT NULL, created_at INTEGER, "
            "sort_index INTEGER, is_current BOOLEAN NOT NULL DEFAULT 0, "
            "PRIMARY KEY (id, app_type))"
        )
        for i, name in enumerate(display_names):
            settings = {
                "env": {
                    "ANTHROPIC_BASE_URL": f"https://example.com/p{i}/v1",
                    "ANTHROPIC_MODEL": f"fake-model-{i}",
                }
            }
            row_id = name_to_id.get(name) or (
                name
                if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                else f"row-{i}"
            )
            conn.execute(
                "INSERT INTO providers (id, app_type, name, settings_config, "
                "created_at, sort_index) VALUES (?, ?, ?, ?, ?, ?)",
                (row_id, "claude", name, json.dumps(settings), i, i),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _install_fake_home_prod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    display_names: List[str],
) -> Path:
    """Install a production-schema fake DB under the fake ``$HOME``."""
    db_path = _make_fake_cc_switch_db_prod(tmp_path, display_names)
    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path.replace(cc_dir / "cc-switch.db")
    monkeypatch.setenv("HOME", str(tmp_path))
    return cc_dir / "cc-switch.db"


#: An authority nothing can be listening on, so the HTTP leg fails the
#: way it does when CC Switch is closed. Used instead of stubbing the
#: leg, so the resolver's real connect-and-classify path runs.
API_DOWN = "http://127.0.0.1:1"


def _api_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDT_PROVIDER_ORDER_API_URL", API_DOWN)


def _api_serving(monkeypatch: pytest.MonkeyPatch, names: List[str]) -> None:
    """Make CC Switch's HTTP provider list answer with ``names``."""
    payload = {"providers": [{"id": f"id-{i}", "name": n}
                             for i, n in enumerate(names)]}
    body = json.dumps(payload).encode()

    class _Resp:
        status = 200

        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import provider_order

    monkeypatch.setattr(
        provider_order.urllib.request, "urlopen", lambda *a, **k: _Resp()
    )


def _collect_bare_provider_literals(source: str) -> Set[ast.AST]:
    """Walk *source* AST and return container nodes that hold bare
    ``"vendor-b"`` / ``"vendor-a"`` string elements.

    A "container node" is a :class:`ast.List`, :class:`ast.Tuple`, or
    :class:`ast.Set`.  :class:`ast.Dict` nodes are also scanned for
    bare strings on either the key or the value side.  Bare means a
    *constant* ``str`` node whose value is exactly ``"vendor-b"`` or
    ``"vendor-a"`` (case-sensitive) — kebab-case IDs like
    ``"vendor-b-pro"`` are not flagged because the consumer layer
    explicitly permits them.
    """
    tree = ast.parse(source)
    bare_ids = {"vendor-b", "vendor-a"}
    flagged: Set[ast.AST] = []

    def _is_bare(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in bare_ids
        )

    def _scan_elements(elt_list) -> bool:
        return any(_is_bare(elt) for elt in elt_list)

    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            if _scan_elements(node.elts):
                flagged.append(node)
        elif isinstance(node, ast.Dict):
            if _scan_elements(node.keys) or _scan_elements(node.values):
                flagged.append(node)
    return set(flagged)


# ---------------------------------------------------------------------------
# 1. Static scan — no hard-coded provider lists/dicts in server.py
# ---------------------------------------------------------------------------


def test_server_no_hardcoded_providers():
    """``backend/server.py`` must not contain a hard-coded
    ``["vendor-b", "vendor-a"]`` (or reverse-order) list/dict literal.

    Pins the Task 7 contract: provider sources flow through the
    consumer layer.  A bare ``"vendor-b"`` or ``"vendor-a"`` literal in a
    container (list/tuple/set/dict) is the smoking-gun signature of
    legacy hard-coded provider lookups.
    """
    source = SERVER_PY.read_text(encoding="utf-8")
    flagged = _collect_bare_provider_literals(source)

    assert not flagged, (
        f"server.py contains hard-coded provider literals — these MUST "
        f"go through provider_order + cc_switch. "
        f"Offending AST nodes: {len(flagged)}"
    )


def test_server_no_hardcoded_provider_attribute():
    """No bare ``"vendor-b"`` / ``"vendor-a"`` strings anywhere in
    ``server.py`` source.

    Stricter companion to :func:`test_server_no_hardcoded_providers`:
    even a *bare* string literal (not nested in a container) is a
    likely legacy hard-coded reference and is therefore a violation.
    Comments and docstrings are scanned too — the static rule is "no
    bare provider names in this file at all".
    """
    source = SERVER_PY.read_text(encoding="utf-8")
    bare_ids = {"vendor-b", "vendor-a"}
    offenders: List[str] = []

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in bare_ids
        ):
            line = getattr(node, "lineno", "?")
            offenders.append(f"line {line}: {node.value!r}")

    assert not offenders, (
        f"server.py contains bare 'vendor-b' or 'vendor-a' string literals — "
        f"these MUST go through the consumer layer. Offenders: {offenders[:5]}"
    )


# ---------------------------------------------------------------------------
# 2. Startup asks CC Switch for the chain
# ---------------------------------------------------------------------------


def test_server_startup_resolves_the_chain_cc_switch_served(
    tmp_path, monkeypatch, caplog, real_cc_switch
):
    """``server.load_providers`` dispatches in the order CC Switch served.

    Boundary contract: the chain is whatever ``provider_order``
    resolved — no file, no re-sort — and each entry is then resolved to
    a config via :func:`cc_switch.get_provider`. The resolved configs
    land in :data:`server.STARTUP_PROVIDERS` in that same order, ready
    for the rest of the server to consume.
    """
    caplog.set_level(logging.INFO, logger="server")
    provider_order_cache_clear()
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    display_order = ["Vendor A", "Vendor B Pro", "vendor-c-app"]
    _install_fake_home_prod(tmp_path, monkeypatch, display_order)
    _api_serving(monkeypatch, display_order)

    try:
        result = server.load_providers()
    finally:
        provider_order_cache_clear()

    assert [cfg.name for cfg in result] == display_order
    # The returned list is the same object as the module-level cache
    # (so callers reading STARTUP_PROVIDERS see the same chain).
    assert [cfg.name for cfg in server.STARTUP_PROVIDERS] == display_order
    # Each resolved config must carry the fields the consumer layer
    # guarantees — a chain entry that resolved to an empty config would
    # dispatch to a provider with no endpoint.
    for cfg in result:
        assert cfg.base_url
        assert cfg.model


def test_server_startup_fails_loudly_when_cc_switch_is_not_serving(
    tmp_path, monkeypatch, real_cc_switch
):
    """Nothing serving is a startup failure, even with a full local store.

    This is the scenario the deleted database leg used to paper over:
    a populated ``~/.cc-switch/cc-switch.db`` next to a closed desktop
    app, which produced a chain and hid the fact that the thing which
    owns the fleet was not answering. It now fails, which is the only
    outcome that tells an operator their CC Switch is down.
    """
    provider_order_cache_clear()
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    display_order = ["Vendor A", "Vendor B Pro", "vendor-c-app"]
    _install_fake_home_prod(tmp_path, monkeypatch, display_order)
    _api_down(monkeypatch)

    try:
        with pytest.raises(ProviderOrderError) as excinfo:
            server.load_providers()
    finally:
        provider_order_cache_clear()

    assert server.STARTUP_PROVIDERS == [], (
        "a failed startup must leave no providers behind, or a later "
        f"read would dispatch on them: {server.STARTUP_PROVIDERS!r}"
    )
    assert "api_unreachable" in str(excinfo.value), (
        f"the failure does not say CC Switch could not be reached: "
        f"{excinfo.value!s}"
    )


def test_server_startup_dispatches_in_the_order_cc_switch_served(
    tmp_path, monkeypatch, real_cc_switch
):
    """The served order wins, not the local store's row order.

    The store carries rows in whatever order it was written; the chain
    is the array CC Switch served. Resolving in store order instead
    would make the operator's reordering appear not to have taken
    effect.
    """
    provider_order_cache_clear()
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    _install_fake_home_prod(tmp_path, monkeypatch, ["Vendor A", "Vendor B Pro"])
    _api_serving(monkeypatch, ["Vendor B Pro", "Vendor A"])

    try:
        result = server.load_providers()
    finally:
        provider_order_cache_clear()

    assert [cfg.name for cfg in result] == ["Vendor B Pro", "Vendor A"]


def test_server_startup_skips_unknown_ids(tmp_path, monkeypatch, caplog, real_cc_switch):
    """A chain entry with no CC Switch row is skipped, not fatal.

    CC Switch can be listing a provider whose row this backend cannot
    resolve. Dropping the one entry keeps the rest of the chain
    dispatchable; failing startup over it would make a single bad row
    take the whole backend down.
    """
    caplog.set_level(logging.INFO, logger="server")
    provider_order_cache_clear()
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    _install_fake_home_prod(tmp_path, monkeypatch, ["Vendor A"])

    # "Ghost Provider" is not in the database and must be skipped.
    _api_serving(monkeypatch, ["Vendor A", "Ghost Provider"])

    try:
        result = server.load_providers()
    finally:
        provider_order_cache_clear()

    # Only the known entry survives; the ghost entry is skipped.
    assert [cfg.name for cfg in result] == ["Vendor A"]
    # A warning is logged for the unknown entry.
    assert any("Ghost Provider" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# 3. Startup fails explicitly when neither CC Switch surface answers
# ---------------------------------------------------------------------------


def test_server_startup_no_db_fails(tmp_path, monkeypatch, real_cc_switch):
    """Neither surface answering is a hard failure.

    Boundary contract: a backend that cannot learn its chain must say
    so at startup. The previous behaviour — warn and continue — is
    exactly how a stale chain reached production dispatch.
    """
    provider_order_cache_clear()
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    # Point $HOME at a directory with no .cc-switch inside.
    fake_home = tmp_path / "empty-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    _api_down(monkeypatch)

    try:
        with pytest.raises((CCSwitchError, ProviderOrderError)):
            server.load_providers()
    finally:
        provider_order_cache_clear()


def test_server_startup_corrupt_db_fails(tmp_path, monkeypatch, real_cc_switch):
    """A database that exists but cannot be read also fails explicitly.

    A zero-byte file at ``$HOME/.cc-switch/cc-switch.db`` is a valid
    path and a corrupt store. :func:`load_providers` must surface that
    rather than swallow it and dispatch on an empty chain.
    """
    provider_order_cache_clear()
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    (cc_dir / "cc-switch.db").write_bytes(b"")  # not a valid SQLite file
    monkeypatch.setenv("HOME", str(tmp_path))
    _api_down(monkeypatch)

    try:
        with pytest.raises((CCSwitchError, ProviderOrderError)):
            server.load_providers()
    finally:
        provider_order_cache_clear()


def test_server_startup_propagates_provider_order_error(
    tmp_path, monkeypatch, real_cc_switch,
):
    """An unresolvable chain reaches the caller as
    :class:`ProviderOrderError`, naming what failed.

    The error is re-raised rather than re-wrapped: the message already
    carries the reason CC Switch could not be asked and the evidence
    behind it, and a second wrapper naming a step that no longer exists
    would bury that.
    """
    provider_order_cache_clear()
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    # A populated local store, so the failure can only come from the
    # chain being unresolvable — not from an empty install.
    _install_fake_home(tmp_path, monkeypatch, ["vendor-a-pro"])
    _install_fake_home_prod(tmp_path, monkeypatch, [])
    _api_down(monkeypatch)

    try:
        with pytest.raises(ProviderOrderError) as excinfo:
            server.load_providers()
    finally:
        provider_order_cache_clear()

    message = str(excinfo.value)
    assert "api_unreachable" in message, (
        f"the error does not say why CC Switch could not be asked: "
        f"{message!r}"
    )
    assert "127.0.0.1:1" in message or "api/providers" in message, (
        f"the error does not point at the endpoint that was tried: "
        f"{message!r}"
    )


# ---------------------------------------------------------------------------
# 4. Companion: ``_resolve_max_parallel`` reads the live consumer state
# ---------------------------------------------------------------------------
#
# After the Task 7 refactor, ``_resolve_max_parallel`` MUST NOT consult
# any hard-coded provider cap map.  These tests pin the contract by
# checking that the function uses the live ``PROVIDER_LIMITS`` table
# populated by the consumer layer.


class _FakeCodingTool:
    """Minimal stand-in for ``coding_tool`` with a ``provider_priority``."""

    def __init__(self, priority: List[str]):
        self.provider_priority = priority


def test_resolve_max_parallel_uses_provider_limits(monkeypatch):
    """``_resolve_max_parallel`` reads caps from the live
    ``PROVIDER_LIMITS`` table populated by the consumer layer.

    Pins the Task 7 contract: the legacy hard-coded
    ``_PROVIDER_CONCURRENCY_CAPS`` map MUST be gone, and the function
    MUST consult ``PROVIDER_LIMITS`` instead.
    """
    monkeypatch.setattr(
        server, "PROVIDER_LIMITS",
        {"vendor-a-pro": 7, "vendor-b-pro": 2},
    )
    assert server._resolve_max_parallel(
        _FakeCodingTool(["vendor-a-pro"])
    ) == 7
    assert server._resolve_max_parallel(
        _FakeCodingTool(["vendor-b-pro"])
    ) == 2


def test_resolve_max_parallel_unknown_provider_uses_default(monkeypatch):
    """A primary provider not present in ``PROVIDER_LIMITS`` returns
    the default cap (5).

    Boundary contract: a provider that the consumer layer has not
    registered (or whose cap the consumer layer does not expose) MUST
    fall back to the default — never to a hard-coded per-provider map.
    """
    monkeypatch.setattr(
        server, "PROVIDER_LIMITS",
        {"vendor-a-pro": 7},
    )
    assert server._resolve_max_parallel(
        _FakeCodingTool(["ghost-provider"])
    ) == 5


def test_resolve_max_parallel_no_hardcoded_caps_map():
    """The legacy hard-coded ``_PROVIDER_CONCURRENCY_CAPS`` map MUST
    NOT exist on the ``server`` module after the refactor.

    Pins the Task 7 contract: any code still referencing the legacy
    map would shadow the live consumer-layer table and silently
    confuse the lookup.
    """
    assert not hasattr(server, "_PROVIDER_CONCURRENCY_CAPS"), (
        "_PROVIDER_CONCURRENCY_CAPS was removed; the consumer layer "
        "(PROVIDER_LIMITS) is the single source of truth"
    )


def test_resolve_max_parallel_empty_priority(monkeypatch):
    """An empty ``provider_priority`` list returns the default cap.

    Boundary contract: when the plan has no provider priority at all,
    the function MUST NOT raise — it returns the default.
    """
    monkeypatch.setattr(
        server, "PROVIDER_LIMITS", {"vendor-a-pro": 7},
    )
    assert server._resolve_max_parallel(_FakeCodingTool([])) == 5
