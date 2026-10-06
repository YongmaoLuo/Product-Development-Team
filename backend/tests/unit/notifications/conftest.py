"""Shared fixtures for the notifier suites.

See ``status_payload.py`` for why these suites still express their
fixtures as three sections while the notifier now makes a single read.
"""

from __future__ import annotations

import pytest

import credentials
from status_payload import build_payload


@pytest.fixture(autouse=True)
def _unmemoised_secret():
    """Give every test its own resolution of every secret.

    ``credentials`` memoises a secret for the life of the process,
    which is right for a deployment and wrong for a test: the
    deployment here is whatever the previous test's ``setenv`` left
    behind. Left alone, the memo makes these suites order-dependent —
    a test that clears ``TELEGRAM_BOT_TOKEN`` hands the next test a
    cached "no token", and one that sets it hands the next test a
    cached "token" that no longer exists. Both are answers about a
    process that is not running any more.

    Resetting on the way in *and* on the way out keeps a resolution
    from escaping into a suite that never asked for it.
    """
    credentials.reset_cache()
    yield
    credentials.reset_cache()


@pytest.fixture
def patch_status_fetch(monkeypatch):
    """Patch the notifier's single status fetch with fixture sections."""

    def _apply(summary=None, execution=None, verification=None, status=None):
        def _fake(plan_id, base_url=None):
            return build_payload(
                plan_id, summary, execution, verification, status,
            )

        monkeypatch.setattr(
            "notifications.feishu_notifier.fetch_plan_status", _fake,
        )
        return _fake

    return _apply
