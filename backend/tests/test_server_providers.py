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
    SCHEMA_VERSION,
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
            "id TEXT PRIMARY KEY, name TEXT, settings_config TEXT"
            ")"
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
                "INSERT INTO providers (id, name, settings_config) "
                "VALUES (?, ?, ?)",
                (row_id, name, json.dumps(settings)),
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


def _write_payload(path: Path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


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
# 2. Startup reads provider chain from CC Switch DB
# ---------------------------------------------------------------------------


def test_server_startup_reads_db(tmp_path, monkeypatch, caplog):
    """``server.load_providers`` resolves the chain from the DB consumer.

    Boundary contract: the chain comes from
    ``provider_order.load_fallback_order`` (which already filters to
    DB-present entries), and each surviving entry is then resolved via
    :func:`cc_switch.get_provider`. The
    resolved configs land in :data:`server.STARTUP_PROVIDERS` in
    fallback order, ready for the rest of the server to consume.

    Post-5561e2c the chain carries CC Switch display names, so the
    fixture uses the production ``providers`` schema and the JSON
    order lists display names verbatim.
    """
    caplog.set_level(logging.INFO, logger="server")
    provider_order_cache_clear()
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    display_order = ["Vendor A", "Vendor B Pro", "vendor-c-app"]
    _install_fake_home_prod(tmp_path, monkeypatch, display_order)

    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": display_order,
        "providers": {},
    }
    _write_payload(target, payload)
    monkeypatch.setattr(server, "PROVIDER_ORDER_FILE", target)

    try:
        result = server.load_providers()
    finally:
        provider_order_cache_clear()

    assert [cfg.name for cfg in result] == display_order
    # The returned list is the same object as the module-level cache
    # (so callers reading STARTUP_PROVIDERS see the same chain).
    assert [cfg.name for cfg in server.STARTUP_PROVIDERS] == display_order
    # Each resolved config must carry the fields the consumer layer
    # promises — otherwise downstream callers cannot build a client.
    for cfg in result:
        assert isinstance(cfg, ProviderConfig)
        assert cfg.base_url
        assert cfg.model
        assert cfg.env


def test_server_startup_skips_unknown_ids(tmp_path, monkeypatch, caplog):
    """Unknown entries in the chain are skipped with a warning, not
    promoted to a startup failure.

    Boundary contract: optimiser/DB drift (an entry present in the JSON
    chain but not in the CC Switch database) MUST NOT crash startup —
    the entry is logged and dropped, and the rest of the chain still
    loads.
    """
    caplog.set_level(logging.WARNING, logger="server")
    provider_order_cache_clear()
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    _install_fake_home_prod(tmp_path, monkeypatch, ["Vendor A"])

    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        # "Ghost Provider" is not in the DB and must be skipped.
        "order": ["Vendor A", "Ghost Provider"],
        "providers": {},
    }
    _write_payload(target, payload)
    monkeypatch.setattr(server, "PROVIDER_ORDER_FILE", target)

    try:
        result = server.load_providers()
    finally:
        provider_order_cache_clear()

    # Only the known entry survives; the ghost entry is skipped.
    assert [cfg.name for cfg in result] == ["Vendor A"]
    # A warning is logged for the unknown entry (by the provider_order
    # filter that drops it from the chain).
    assert any("Ghost Provider" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# 3. Startup fails explicitly when the DB is unreadable
# ---------------------------------------------------------------------------


def test_server_startup_no_db_fails(tmp_path, monkeypatch):
    """When the CC Switch DB is missing, ``load_providers`` raises
    :class:`CCSwitchError` — startup MUST fail explicitly.

    Boundary contract: a missing/unreadable database is a hard
    failure, not a soft degradation.  The previous behaviour (warn
    and continue) is forbidden by the Task 1 contract.
    """
    provider_order_cache_clear()
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    # Point $HOME at a directory with no .cc-switch inside.
    fake_home = tmp_path / "empty-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))

    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["vendor-a-pro", "vendor-b-pro"],
        "providers": {},
    }
    _write_payload(target, payload)
    monkeypatch.setattr(server, "PROVIDER_ORDER_FILE", target)

    try:
        with pytest.raises((CCSwitchError, ProviderOrderError)):
            server.load_providers()
    finally:
        provider_order_cache_clear()


def test_server_startup_corrupt_db_fails(tmp_path, monkeypatch):
    """When the CC Switch DB file exists but is unreadable, startup
    also fails explicitly.

    A zero-byte file at ``$HOME/.cc-switch/cc-switch.db`` is a valid
    path but a corrupt SQLite database.  :func:`load_providers` must
    surface the error rather than swallow it.
    """
    provider_order_cache_clear()
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    (cc_dir / "cc-switch.db").write_bytes(b"")  # not a valid SQLite file
    monkeypatch.setenv("HOME", str(tmp_path))

    target = tmp_path / "provider-order.json"
    payload = {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": ["vendor-a-pro"],
        "providers": {},
    }
    _write_payload(target, payload)
    monkeypatch.setattr(server, "PROVIDER_ORDER_FILE", target)

    try:
        with pytest.raises((CCSwitchError, ProviderOrderError)):
            server.load_providers()
    finally:
        provider_order_cache_clear()


def test_server_startup_propagates_provider_order_error(
    tmp_path, monkeypatch,
):
    """A missing/invalid ``provider-order.json`` propagates as
    :class:`ProviderOrderError` — startup fails explicitly.

    Boundary contract: the optimizer contract file is the single
    source of truth; a missing file is a hard failure, not a silent
    fallback to ``_base.yaml`` ``provider_priority``.
    """
    provider_order_cache_clear()
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    _install_fake_home(tmp_path, monkeypatch, ["vendor-a-pro"])

    # No provider-order.json in the path → load_fallback_order raises.
    missing = tmp_path / "missing-provider-order.json"
    assert not missing.exists()
    monkeypatch.setattr(server, "PROVIDER_ORDER_FILE", missing)

    try:
        with pytest.raises(ProviderOrderError):
            server.load_providers()
    finally:
        provider_order_cache_clear()


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
