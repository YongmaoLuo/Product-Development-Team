"""
Tests for verification_config.TimeoutPolicy and the verification yaml
loading path on ConfigRegistry.

The four required test cases are:
- test_timeout_policy_yaml_loading_defaults
- test_timeout_policy_per_vp_override_wins
- test_timeout_policy_unknown_method_falls_back_to_global
- test_config_registry_loads_verification_yaml
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from verification_config import (
    DEFAULT_GLOBAL_TIMEOUT_SECONDS,
    DEFAULT_PARALLELISM_CAP,
    DEFAULT_PER_METHOD_TIMEOUT_SECONDS,
    TimeoutPolicy,
)


# -----------------------------------------------------------------------------
# TimeoutPolicy construction
# -----------------------------------------------------------------------------


class TestTimeoutPolicyFromYamlDefaults:
    """Missing / malformed yaml must produce the hard-coded defaults."""

    def test_timeout_policy_yaml_loading_defaults(self, tmp_path, monkeypatch):
        """Point TimeoutPolicy.from_yaml at a non-existent path → defaults.

        Mirrors the spec: ``policy.resolve("ui_validation")`` on a
        policy built from a missing yaml must return 3600 (the
        hard-coded fallback documented in DEFAULT_PER_METHOD_TIMEOUT_SECONDS).
        """
        missing = tmp_path / "does_not_exist.yaml"
        assert not missing.exists()

        policy = TimeoutPolicy.from_yaml(str(missing))

        # Sanity: the method map was populated from hard-coded defaults.
        assert policy.per_method() == dict(DEFAULT_PER_METHOD_TIMEOUT_SECONDS)
        assert policy.global_default() == DEFAULT_GLOBAL_TIMEOUT_SECONDS
        assert policy.parallelism_cap == DEFAULT_PARALLELISM_CAP

        # Spec assertion: per-method resolve still works.
        assert policy.resolve("ui_validation") == 3600
        assert policy.resolve("code_review") == 3600
        assert policy.resolve("automated_test") == 3600
        assert policy.resolve("api_test") == 3600


class TestTimeoutPolicyFromYamlLoadsReal:
    """When the real yaml exists, the values from it are picked up."""

    def test_from_yaml_uses_values_from_file(self):
        """The on-disk backend/configs/verification.yaml is the source of truth."""
        real_yaml = Path(__file__).parent.parent / "configs" / "verification.yaml"
        if not real_yaml.exists():
            pytest.skip("backend/configs/verification.yaml not present in this checkout")

        policy = TimeoutPolicy.from_yaml(str(real_yaml))

        # The numbers declared in verification.yaml.
        # 2026-09-13: per-VP override deleted — flat 1-hour cap only.
        assert policy.resolve("ui_validation") == 3600
        assert policy.resolve("code_review") == 3600
        assert policy.resolve("automated_test") == 3600
        assert policy.resolve("api_test") == 3600
        assert policy.parallelism_cap == 4


# -----------------------------------------------------------------------------
# Per-VP override deleted (2026-09-13 plan)
# -----------------------------------------------------------------------------


class TestTimeoutPolicyPerVPOverrideDeleted:
    """2026-09-13: the per-VP ``timeout_seconds`` override was
    DELETED. ``resolve()`` is method-level only — a VP payload value
    can never change the returned timeout."""

    def test_resolve_signature_has_no_vp_payload(self):
        """resolve() takes only verification_method (no vp_payload param)."""
        import inspect

        sig = inspect.signature(TimeoutPolicy.resolve)
        params = [p for p in sig.parameters if p != "self"]
        assert params == ["verification_method"], (
            f"resolve() signature changed: {params}. The vp_payload "
            f"override was deleted 2026-09-13; restore it only with an "
            f"the contract is explicit."
        )

    def test_resolve_ignores_legacy_payload_field(self):
        """Even if a legacy payload dict still carries timeout_seconds,
        resolve() must ignore it (it is no longer read)."""
        policy = TimeoutPolicy.defaults()

        # These calls would have returned the override value before.
        # Now: method-level resolution only → 3600.
        assert policy.resolve("ui_validation") == 3600
        assert policy.resolve("automated_test") == 3600
        assert policy.resolve("api_test") == 3600


# -----------------------------------------------------------------------------
# Unknown method fallback
# -----------------------------------------------------------------------------


class TestTimeoutPolicyUnknownMethod:
    """An unknown verification_method must NOT raise KeyError."""

    def test_timeout_policy_unknown_method_falls_back_to_global(self):
        """Resolve on an unknown method → global default (3600), no raise."""
        policy = TimeoutPolicy.defaults()

        # The spec example: unknown_method → 3600.
        assert policy.resolve("unknown_method") == 3600

        # A few more unknown methods, all hitting the same fallback.
        for method in ("manual_check", "fuzzy_match", "", "UPPERCASE", "  "):
            assert policy.resolve(method) == 3600, (
                f"method {method!r} should fall back to global default"
            )


# -----------------------------------------------------------------------------
# ConfigRegistry wiring
# -----------------------------------------------------------------------------


class TestConfigRegistryVerification:
    """ConfigRegistry must expose the verification yaml as a registry entry."""

    def test_config_registry_loads_verification_yaml(self):
        """``ConfigRegistry.get('verification')`` returns the execution block.

        Mirrors the spec: at startup, ``ConfigRegistry.get('verification')``
        must contain the ``execution`` field. This is what guarantees
        that downstream callers can resolve ``TimeoutPolicy.from_dict``
        on the registered value without re-parsing the yaml themselves.
        """
        from config_registry import ConfigRegistry

        registered = ConfigRegistry.get("verification")

        # The registry must be populated, not None.
        assert registered is not None, (
            "ConfigRegistry.get('verification') returned None — "
            "_register_verification_yaml did not run or failed silently"
        )

        # The shape must be a dict (we register the raw parsed yaml).
        assert isinstance(registered, dict), (
            f"Expected dict, got {type(registered).__name__}"
        )

        # The 'execution' block is the spec contract.
        assert "execution" in registered, (
            f"Expected 'execution' key in {sorted(registered.keys())}"
        )
        execution = registered["execution"]
        assert isinstance(execution, dict)

        # Spot-check the per-method map so we know yaml actually parsed
        # (and didn't get replaced with an empty dict by the error path).
        # 2026-09-18: derived from SUPPORTED_METHODS rather than a
        # hard-coded list — the hard-coded one still named the retired
        # ``automated_test``, so it silently stopped checking anything
        # real once that method went away.
        from verification_subagent import SUPPORTED_METHODS

        per_method = execution.get("per_method_timeout_seconds", {})
        assert isinstance(per_method, dict)
        for method in SUPPORTED_METHODS:
            assert method in per_method, (
                f"Expected {method!r} in per_method_timeout_seconds; "
                f"got {sorted(per_method.keys())}"
            )

        # And parallelism_cap should also be present.
        assert "parallelism_cap" in execution

    def test_config_registry_verification_resolves_to_expected_timeouts(self):
        """End-to-end: registry → TimeoutPolicy.from_dict → resolve."""
        from config_registry import ConfigRegistry

        registered = ConfigRegistry.get("verification")
        policy = TimeoutPolicy.from_dict(registered)

        # Same expectations as test_from_yaml_uses_values_from_file,
        # but driven via the registry rather than the on-disk path.
        # 2026-09-13: per-VP override deleted — flat 1-hour cap only.
        assert policy.resolve("ui_validation") == 3600
        assert policy.resolve("code_review") == 3600
        assert policy.resolve("automated_test") == 3600
        assert policy.resolve("api_test") == 3600
        assert policy.parallelism_cap == 4
