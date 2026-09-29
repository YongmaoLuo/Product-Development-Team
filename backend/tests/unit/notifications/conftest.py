"""Shared fixtures for the notifier suites.

See ``status_payload.py`` for why these suites still express their
fixtures as three sections while the notifier now makes a single read.
"""

from __future__ import annotations

import pytest

from status_payload import build_payload


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
