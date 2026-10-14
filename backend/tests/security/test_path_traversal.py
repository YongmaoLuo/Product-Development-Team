"""Path-traversal protection for the one file the provider layer reads.

The fallback chain is no longer read from disk at all — it is asked of
CC Switch — so ``load_fallback_order`` takes no path and has nothing to
traverse. The file this module is about is the 5-hour quota payload,
which is still read from ``<config dir>/provider-order.json`` and whose
path arrives from an argument or an environment variable, either of
which an operator (or a mistake) can point anywhere.

Both guards below are worth keeping as the read survives: ``..`` is
rejected before any filesystem call, and a resolved path outside the
project root or the system temp directory is rejected after.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from provider_order import load_provider_5h_usage


SAMPLE_USAGE = {
    "providers": {
        "Vendor A Pro": {"five_hour_remaining_pct": 90.0},
        "Vendor B Pro API": {"five_hour_remaining_pct": 12.5},
    }
}


def test_rejects_path_with_dotdot_component():
    """Paths with '..' are rejected before any file access."""
    with pytest.raises(ValueError, match="\\.\\."):
        load_provider_5h_usage("/tmp/../etc/passwd")


def test_rejects_absolute_path_outside_allowed_roots():
    """Absolute paths to system files like /etc/passwd are rejected."""
    with pytest.raises(ValueError, match="outside allowed"):
        load_provider_5h_usage("/etc/passwd")


def test_allows_normal_absolute_path_in_tmp(tmp_path):
    """A plain absolute path under the temp directory is accepted.

    The rejection cases above are only meaningful next to an acceptance
    case: a guard that refuses everything passes both of them.
    """
    target = tmp_path / "provider-order.json"
    target.write_text(json.dumps(SAMPLE_USAGE), encoding="utf-8")

    assert load_provider_5h_usage(target) == {
        "Vendor A Pro": 90.0,
        "Vendor B Pro API": 12.5,
    }


def test_allows_normal_absolute_path_under_the_project(tmp_path):
    """The project root is the other allowed root.

    ``_PROJECT_ROOT`` is allowed because the default config directory
    lives there, so a deployment pointing ``PDT_PROVIDER_USAGE_FILE``
    at a sibling config tree has to keep working.
    """
    import provider_order

    root = provider_order._PROJECT_ROOT
    target = root / ".config" / "provider-order.json"
    if target.parent.exists():
        target.write_text(json.dumps(SAMPLE_USAGE), encoding="utf-8")
        try:
            assert load_provider_5h_usage(target)["Vendor A Pro"] == 90.0
        finally:
            target.unlink(missing_ok=True)
    else:
        pytest.skip("no .config directory in this checkout")


def test_a_traversing_path_is_refused_before_it_is_read(monkeypatch):
    """The guard is on the path, not on what the read made of it.

    With a real ``..`` path handed in, no file is opened regardless of
    what would have been found there — the check is a property of the
    path string, so a symlink-free world and a symlink-heavy one behave
    the same.
    """
    import provider_order

    opened = []
    monkeypatch.setattr(
        provider_order.Path, "read_text", lambda self, **kw: opened.append(self)
    )

    with pytest.raises(ValueError):
        load_provider_5h_usage("/tmp/../etc/passwd")

    assert opened == []
