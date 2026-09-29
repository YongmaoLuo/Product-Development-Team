"""Guard: ``backend/configs/_base.yaml`` must not declare a hardcoded
provider order at the top level.

The runtime fallback chain is supplied by an external producer's
contract file plus the CC Switch DB read on top of it — never by a
static YAML key. A top-level ``provider_order`` /
``provider_priority`` / ``provider_fallback_order`` would silently
override the runtime decision and re-introduce the "two sources of
truth" bug the migration eliminated.

Companion test: :mod:`backend.tests.unit.test_base_yaml_naming`
covers the kebab-case / naming rules; this test covers the
"no top-level order key" rule only.
"""
from __future__ import annotations

from pathlib import Path
import yaml

_CONFIG_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "configs"
    / "_base.yaml"
)

_FORBIDDEN_ORDER_KEYS = (
    "provider_order",
    "provider_priority",
    "provider_fallback_order",
    "provider_fallback_chain",
)


def _load_base_yaml() -> dict:
    with _CONFIG_PATH.open("r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp)
    assert isinstance(data, dict), f"_base.yaml root must be a dict, got {type(data)}"
    return data


def test_base_yaml_no_top_level_order_key():
    """_base.yaml must not declare a top-level provider order key."""
    data = _load_base_yaml()
    leaked = [k for k in _FORBIDDEN_ORDER_KEYS if k in data]
    assert not leaked, (
        f"_base.yaml declares hardcoded provider order key(s) {leaked!r}; "
        "the runtime fallback chain must come from "
        "the producer's contract file or the CC Switch DB, "
        "not from this static config."
    )


# 2026-09-23: ``test_base_yaml_runtime_priority_is_optimizer_or_db`` was
# deleted here. It called ``load_fallback_order()`` and asserted only
# ``chain`` non-empty and ``all(isinstance(p, str))`` — i.e. it asserted
# that the runtime artifact existed, not that any backend code behaves
# correctly. Its docstring's stated contract ("the priority must not
# come from a ``provider_priority`` field in ``_base.yaml``") is already
# enforced above by ``test_base_yaml_no_top_level_order_key``, which
# reads the YAML directly and needs no runtime file.
#
# Concretely it failed on every fresh CI checkout, where
# ``the optimizer state directory `` is absent (gitignored, so the
# optimiser's runtime state never ships), while passing on every
# developer machine. The chain's *content* — optimizer-written JSON
# filtered through the CC Switch DB — is covered where it belongs, by
# ``tests/integration/test_provider_order_integration.py`` (VP-008),
# against a fixture it writes itself.
