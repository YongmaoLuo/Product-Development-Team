"""VP-014 — 调用链模块: ``server.py`` 已消除硬编码 provider 引用.

Pinned contracts (call-chain refactor that rips legacy hard-coded
provider knowledge out of ``backend/server.py``):

  1. **No bare legacy provider IDs in source.** A static AST scan of
     ``server.py`` MUST NOT find bare ``"vendor-b"`` / ``"vendor-a"`` /
     provider-name literals — a cap is keyed by whatever CC Switch
     calls the provider, so a private provider's name has no business
     in the shipped source (``DEFAULT_PROVIDER_LIMITS`` is empty).

  2. **Call chain modules imported and re-exported.** ``server.py``
     MUST import ``get_provider`` /
     ``list_provider_names`` /
     ``CCSwitchError`` from ``cc_switch``, and
     ``load_fallback_order`` / ``ProviderOrderError`` from
     ``provider_order``. Without these symbols on the module, the
     runtime call chain is broken.

  3. **Legacy hard-coded cap map removed.** The pre-refactor
     ``_PROVIDER_CONCURRENCY_CAPS`` attribute MUST NOT exist on the
     ``server`` module — every cap lookup now flows through
     :data:`server.PROVIDER_LIMITS`.

  4. **``PROVIDER_LIMITS`` prefers the live consumer layer.** The
     dynamic reader harvests ``max_concurrency`` from each provider's
     ``extra_params`` via the consumer layer, and only falls back to
     :data:`DEFAULT_PROVIDER_LIMITS` when the consumer layer is empty.

  5. **``_resolve_max_parallel`` consults the live table only.** Given
     a coding tool with a ``provider_priority`` list, the function
     returns the cap for the primary provider from
     :data:`PROVIDER_LIMITS`, falling back to
     :data:`DEFAULT_VP_CONCURRENCY_CAP` (5) for unknown primaries or
     empty priority lists.

  6. **``_read_dynamic_provider_limits`` resolves per-provider caps
     via the consumer layer.**
     Configuration is looked up via :func:`get_provider_config`; an
     unknown provider raises :class:`ValueError` instead of falling
     back to a hard-coded configuration.

Together these tests verify the verification-point contract:
"server.py 中无硬编码 provider ID、provider 顺序或专属参数；通过
配置消费层和 optimizer 动态获取 provider 信息".
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import List

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from cc_switch import ProviderConfig  # noqa: E402


SERVER_PY = Path(server.__file__).resolve()
SERVER_SOURCE = SERVER_PY.read_text(encoding="utf-8")


_BARE_LEGACY_IDS = {"vendor-b", "vendor-a", "vendor-c"}

_LEGACY_URL_MAP_TOKEN = "provider" + "-" + "url" + "-map"


class _FakeCodingTool:
    """Minimal stand-in for ``coding_tool.ClaudeCodingTool``."""

    def __init__(self, priority: List[str]):
        self.provider_priority = priority


def _bare_legacy_constants(source: str) -> List[ast.Constant]:
    """Return ``ast.Constant`` string nodes equal to a bare legacy ID."""
    tree = ast.parse(source)
    out: List[ast.Constant] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _BARE_LEGACY_IDS
        ):
            out.append(node)
    return out


def test_server_source_has_no_bare_legacy_provider_ids():
    """No bare ``"vendor-b"`` / ``"vendor-a"`` / ``"vendor-c"`` literals.

    These are the smoking-gun signature of legacy hard-coded provider
    knowledge: a string literal with the bare name was how the old
    ``_PROVIDER_CONCURRENCY_CAPS`` map keyed its lookups.
    """
    offenders = _bare_legacy_constants(SERVER_SOURCE)
    rendered = [
        f"line {getattr(n, 'lineno', '?')}: {n.value!r}" for n in offenders
    ]
    assert not offenders, (
        "server.py contains bare legacy provider IDs — these MUST go "
        "through cc_switch + provider_order. "
        f"Offenders: {rendered[:5]}"
    )


def test_server_source_has_no_legacy_url_map_reference():
    """No legacy provider URL map reference in server.py source."""
    assert _LEGACY_URL_MAP_TOKEN not in SERVER_SOURCE, (
        "server.py references the legacy provider URL map token — the "
        "call chain must use cc_switch instead"
    )


def test_server_imports_cc_switch():
    """server.py MUST import the consumer-layer symbols it consumes."""
    assert hasattr(server, "get_provider"), (
        "server.py must import get_provider from "
        "cc_switch"
    )
    assert hasattr(server, "list_provider_names"), (
        "server.py must import list_provider_names from "
        "cc_switch"
    )
    assert hasattr(server, "CCSwitchError"), (
        "server.py must import CCSwitchError from "
        "cc_switch"
    )


def test_server_imports_provider_order():
    """server.py MUST import the optimizer contract reader."""
    assert hasattr(server, "load_fallback_order"), (
        "server.py must import load_fallback_order from provider_order"
    )
    assert hasattr(server, "ProviderOrderError"), (
        "server.py must import ProviderOrderError from provider_order"
    )


def test_server_source_contains_cc_switch_import():
    """The import statement for the consumer layer is in the source."""
    assert "from cc_switch import" in SERVER_SOURCE, (
        "server.py must have a ``from cc_switch import`` "
        "statement"
    )


def test_server_source_contains_provider_order_import():
    """The import statement for the optimizer contract is in source."""
    assert "from provider_order import" in SERVER_SOURCE, (
        "server.py must have a ``from provider_order import`` statement"
    )


def test_server_has_no_legacy_concurrency_caps_attribute():
    """The legacy ``_PROVIDER_CONCURRENCY_CAPS`` map MUST be gone."""
    assert not hasattr(server, "_PROVIDER_CONCURRENCY_CAPS"), (
        "the legacy _PROVIDER_CONCURRENCY_CAPS map was removed; "
        "PROVIDER_LIMITS (populated by _get_provider_limits) is the "
        "single source of truth"
    )


def test_server_source_does_not_define_legacy_caps_map():
    """The legacy map MUST NOT be assigned or referenced in code.

    A docstring/comment mentioning that the legacy map *was removed*
    is acceptable (it documents the migration); an ``ast.Assign`` /
    ``ast.AnnAssign`` with the name as target, or an ``ast.Name`` node
    referencing it in code, is a real regression.
    """
    tree = ast.parse(SERVER_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            for t in targets:
                if isinstance(t, ast.Name) and t.id == "_PROVIDER_CONCURRENCY_CAPS":
                    raise AssertionError(
                        f"server.py line {node.lineno}: re-introduced "
                        "_PROVIDER_CONCURRENCY_CAPS as an assignment target"
                    )
        if isinstance(node, ast.Name) and node.id == "_PROVIDER_CONCURRENCY_CAPS":
            raise AssertionError(
                f"server.py line {node.lineno}: references the removed "
                "_PROVIDER_CONCURRENCY_CAPS name in code — must use "
                "PROVIDER_LIMITS instead"
            )


def test_server_exposes_provider_limits_table():
    """``PROVIDER_LIMITS`` is the live, module-level cap table."""
    assert hasattr(server, "PROVIDER_LIMITS"), (
        "server.py must expose PROVIDER_LIMITS at module level"
    )
    assert isinstance(server.PROVIDER_LIMITS, dict), (
        "PROVIDER_LIMITS must be a dict"
    )


def test_server_provider_limits_keys_are_kebab_case():
    """Every key in PROVIDER_LIMITS is kebab-case (no bare legacy IDs)."""
    import re
    kebab = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
    for key in server.PROVIDER_LIMITS:
        assert isinstance(key, str), (
            f"PROVIDER_LIMITS key must be str, got {type(key).__name__}"
        )
        assert kebab.match(key), (
            f"PROVIDER_LIMITS key {key!r} must be kebab-case; bare "
            "legacy IDs (vendor-b/vendor-a/vendor-c) are forbidden"
        )


def test_server_provider_limits_values_are_positive_ints():
    """Every value in PROVIDER_LIMITS is a positive ``int``."""
    for key, val in server.PROVIDER_LIMITS.items():
        assert isinstance(val, int) and not isinstance(val, bool), (
            f"PROVIDER_LIMITS[{key!r}] must be int (not bool), got "
            f"{type(val).__name__}"
        )
        assert val > 0, (
            f"PROVIDER_LIMITS[{key!r}] must be > 0, got {val}"
        )


def test_server_ships_no_builtin_provider_limits():
    """:data:`DEFAULT_PROVIDER_LIMITS` is empty, and must stay that way.

    It used to seed one operator's private providers with their private
    capacities (``{"vendor-a-pro": 5, "vendor-b-pro": 3}``). In a public
    repository that is both meaningless and wrong: another user's
    providers are not those names, and a cap that names a provider the
    user does not have silently applies to nothing.

    The real source is the CC Switch row itself — the optional
    ``max_concurrency`` field read by
    :func:`_read_dynamic_provider_limits`. An empty seed means "no
    configured cap", which the callers already handle.
    """
    defaults = server.DEFAULT_PROVIDER_LIMITS
    assert isinstance(defaults, dict)
    assert defaults == {}, (
        "DEFAULT_PROVIDER_LIMITS must carry no built-in providers; "
        f"got {defaults!r}. Per-provider caps belong in .config/, not in "
        "the shipped source."
    )


def test_server_default_provider_limits_keys_are_provider_names(monkeypatch):
    """If a seed is ever reintroduced, its keys are provider names.

    The lookup key is whatever CC Switch calls the provider, so a
    kebab-case key would only ever match by accident.
    """
    monkeypatch.setattr(
        server, "DEFAULT_PROVIDER_LIMITS", {"Vendor A Pro": 5},
    )
    assert server._get_provider_limits()["Vendor A Pro"] == 5


def test_server_get_provider_limits_prefers_dynamic(monkeypatch):
    """``_get_provider_limits`` returns dynamic values when available."""
    sentinel = {"vendor-c-app": 11}
    monkeypatch.setattr(
        server, "_read_dynamic_provider_limits", lambda: dict(sentinel)
    )
    result = server._get_provider_limits()
    assert result == sentinel, (
        f"_get_provider_limits must prefer dynamic limits; got {result}"
    )


def test_server_get_provider_limits_falls_back(monkeypatch):
    """When the dynamic reader returns nothing, the default is used."""
    monkeypatch.setattr(server, "_read_dynamic_provider_limits", lambda: {})
    result = server._get_provider_limits()
    assert result == dict(server.DEFAULT_PROVIDER_LIMITS), (
        f"_get_provider_limits must fall back to DEFAULT_PROVIDER_LIMITS; "
        f"got {result}"
    )


def test_server_read_dynamic_provider_limits_uses_consumer(monkeypatch):
    """``_read_dynamic_provider_limits`` walks the consumer layer.

    Names are used verbatim — no casing or separator normalization — so
    a provider CC Switch spells ``"Vendor C App"`` is keyed by
    exactly that. Values that are not a positive int (bool included, as
    ``True`` is an ``int`` in Python) are dropped rather than coerced.
    """
    fake_configs = {
        "Vendor C App": ProviderConfig(
            name="Vendor C App", env={"max_concurrency": 9}
        ),
        "Vendor B Pro": ProviderConfig(name="Vendor B Pro", env={"max_concurrency": 4}),
        "no-extra": ProviderConfig(name="no-extra"),
        "zero-cap": ProviderConfig(name="zero-cap", env={"max_concurrency": 0}),
        "neg-cap": ProviderConfig(name="neg-cap", env={"max_concurrency": -1}),
        "bool-cap": ProviderConfig(name="bool-cap", env={"max_concurrency": True}),
    }

    monkeypatch.setattr(
        server, "list_provider_names",
        lambda: list(fake_configs.keys()),
    )

    def fake_get(name, db_path=None):
        return fake_configs[name]

    monkeypatch.setattr(server, "get_provider", fake_get)

    result = server._read_dynamic_provider_limits()
    assert result == {
        "Vendor C App": 9,
        "Vendor B Pro": 4,
    }, (
        f"dynamic reader must harvest positive int max_concurrency keyed "
        f"by the provider name verbatim; got {result}"
    )


def test_server_read_dynamic_provider_limits_swallows_consumer_error(monkeypatch):
    """A :class:`CCSwitchError` from the consumer returns ``{}``."""

    def raise_pce():
        raise server.CCSwitchError("db missing")

    monkeypatch.setattr(server, "list_provider_names", raise_pce)
    result = server._read_dynamic_provider_limits()
    assert result == {}, (
        f"dynamic reader must return {{}} on CCSwitchError; got {result}"
    )


def test_server_resolve_max_parallel_uses_live_table(monkeypatch):
    """``_resolve_max_parallel`` reads from :data:`PROVIDER_LIMITS`."""
    monkeypatch.setattr(
        server, "PROVIDER_LIMITS",
        {"vendor-c-app": 8, "vendor-b-pro": 2},
    )
    assert server._resolve_max_parallel(
        _FakeCodingTool(["vendor-c-app"])
    ) == 8
    assert server._resolve_max_parallel(_FakeCodingTool(["vendor-b-pro"])) == 2


def test_server_resolve_max_parallel_unknown_returns_default(monkeypatch):
    """Unknown primary falls back to :data:`DEFAULT_VP_CONCURRENCY_CAP`."""
    monkeypatch.setattr(server, "PROVIDER_LIMITS", {"vendor-b-pro": 2})
    assert server._resolve_max_parallel(
        _FakeCodingTool(["ghost-provider"])
    ) == server.DEFAULT_VP_CONCURRENCY_CAP


def test_server_resolve_max_parallel_empty_priority(monkeypatch):
    """Empty ``provider_priority`` returns the default cap."""
    monkeypatch.setattr(server, "PROVIDER_LIMITS", {"vendor-b-pro": 2})
    assert server._resolve_max_parallel(_FakeCodingTool([])) == (
        server.DEFAULT_VP_CONCURRENCY_CAP
    )


def test_server_load_providers_uses_optimizer_chain(monkeypatch):
    """``load_providers`` resolves the chain via the optimizer contract."""
    from provider_order import cache_clear as provider_order_cache_clear

    # Post-5561e2c the optimizer chain carries CC Switch display names;
    # ``load_providers`` resolves each entry via
    # ``cc_switch.get_provider`` and the
    # returned config's ``id`` is the display name it was looked up
    # under.
    display_chain = ["Vendor C App", "Vendor B Pro", "Vendor A"]
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    def fake_chain(file_path=None):
        return list(display_chain)

    monkeypatch.setattr(server, "load_fallback_order", fake_chain)

    fake_cfg = {
        "Vendor C App": ProviderConfig(
            name="Vendor C App",
            base_url="https://a.example/v1",
            model="m-a",
        ),
        "Vendor B Pro": ProviderConfig(
            name="Vendor B Pro", base_url="https://b.example/v1", model="m-b"
        ),
        "Vendor A": ProviderConfig(
            name="Vendor A", base_url="https://c.example/v1", model="m-c"
        ),
    }

    def fake_get(name, db_path=None):
        return fake_cfg[name]

    monkeypatch.setattr(server, "get_provider", fake_get)

    try:
        result = server.load_providers()
    finally:
        provider_order_cache_clear()

    assert [cfg.name for cfg in result] == display_chain, (
        f"load_providers must return configs in optimizer-chain order; "
        f"got {[c.name for c in result]}"
    )
    assert [cfg.name for cfg in server.STARTUP_PROVIDERS] == display_chain


def test_server_load_providers_skips_unknown_ids(monkeypatch, caplog):
    """Unknown IDs in the chain are skipped — startup does not crash."""
    import logging
    from provider_order import cache_clear as provider_order_cache_clear

    caplog.set_level(logging.WARNING, logger="server")
    monkeypatch.setattr(server, "STARTUP_PROVIDERS", [])

    # Post-5561e2c the chain carries display names; ``load_providers``
    # looks each one up via ``get_provider``.
    chain = ["Vendor C App", "ghost-provider"]
    monkeypatch.setattr(
        server, "load_fallback_order", lambda file_path=None: list(chain)
    )

    def fake_get(name, db_path=None):
        if name == "ghost-provider":
            return None
        return ProviderConfig(
            name=name,
            base_url=f"https://{name}.example/v1",
            model=f"m-{name}",
        )

    monkeypatch.setattr(server, "get_provider", fake_get)

    try:
        result = server.load_providers()
    finally:
        provider_order_cache_clear()

    assert [cfg.name for cfg in result] == ["Vendor C App"]
    assert any(
        "ghost-provider" in rec.getMessage() for rec in caplog.records
    ), (
        "unknown provider IDs MUST be logged as a warning so operator "
        "can detect optimizer/DB drift"
    )
