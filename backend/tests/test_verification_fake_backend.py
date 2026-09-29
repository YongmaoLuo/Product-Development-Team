"""
Unit tests for the dry-run ``FakeVerifierBackend`` and the
``_select_backend()`` factory.

The fake backend is used when the environment variable
``VERIFICATION_PROFILE`` is set to ``"dry_run"``. It replaces the
expensive per-method executors (pytest subprocess, Claude code review
subprocess, puppeteer UI validation) with a pure-asyncio
``asyncio.sleep`` so the rest of the verification pipeline (real
events, real group partitioning, real split decision) can be exercised
end-to-end without the LLM or filesystem costs.

These tests pin the contract on three axes:

1. **Happy path** — a fake sleep well under the timeout must surface
   as a ``PASSED`` result, so the rest of the pipeline (orchestrator,
   report, repair task generator) still sees a result it can reason
   about.
2. **Timeout path** — when ``fake_sleep_seconds`` exceeds the per-VP
   ``timeout_seconds``, the fake backend must raise
   :class:`asyncio.TimeoutError` so the same code path that handles
   a real hang (split-on-timeout) is exercised.
3. **Factory selection** — the env-var-controlled switch must default
   to the real backend and only swap in the fake when explicitly
   requested, otherwise every CI run would silently start using the
   fake.
"""

import asyncio
import os

import pytest


# Make `verification_agent` importable when pytest is launched from
# either the project root or the `backend/` directory.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from verification_agent import FakeVerifierBackend, _select_backend  # noqa: E402


# -----------------------------------------------------------------------------
# Happy path: sleeps and returns PASSED
# -----------------------------------------------------------------------------


class TestFakeBackendHappyPath:
    """``fake_sleep < timeout`` → status=``PASSED``."""

    def test_fake_backend_sleeps_then_returns_passed(self):
        """fake_sleep=0.5s, timeout=1800s → PASSED, well under the cap.

        Uses a 0.5s sleep so the test completes in well under a second
        even on slow CI, while still being a non-trivial duration
        that exercises the ``asyncio.sleep`` path rather than a
        trivial no-op.
        """
        backend = FakeVerifierBackend(fake_sleep_seconds=0.5, should_fail=False)
        vp = {"id": "VP-001", "verification_method": "automated_test"}

        result = asyncio.run(backend.execute(vp, timeout_seconds=1800))

        assert result["status"] == "PASSED"
        assert result["id"] == "VP-001"

    def test_fake_backend_should_fail_returns_failed(self):
        """``should_fail=True`` short-circuits to FAILED regardless of sleep.

        Useful for testing the repair-task generation path without
        needing a real failing test.
        """
        backend = FakeVerifierBackend(fake_sleep_seconds=0.1, should_fail=True)
        vp = {"id": "VP-FAIL", "verification_method": "automated_test"}

        result = asyncio.run(backend.execute(vp, timeout_seconds=1800))

        assert result["status"] == "FAILED"

    def test_fake_backend_default_sleep_is_one_second(self):
        """The default ``fake_sleep_seconds`` is 1.0s.

        Pinned because callers that don't pass an explicit value rely
        on this number to size their test budgets.
        """
        backend = FakeVerifierBackend()
        assert backend.fake_sleep_seconds == 1.0
        assert backend.should_fail is False


# -----------------------------------------------------------------------------
# Timeout path: sleep exceeds the cap
# -----------------------------------------------------------------------------


class TestFakeBackendTimeout:
    """``fake_sleep >= timeout`` → raises :class:`asyncio.TimeoutError`."""

    def test_fake_backend_raises_timeout_when_sleep_exceeds(self):
        """fake_sleep=1900, timeout=1800 → asyncio.TimeoutError.

        The fake backend wraps ``asyncio.sleep`` in
        :func:`asyncio.wait_for` so the per-VP timeout boundary is
        observed at the same point as the real backend's. The
        exception propagates so the agent's split-on-timeout branch
        (the path that synthesises a ``SPLIT`` parent result) is
        exercised end-to-end.
        """
        backend = FakeVerifierBackend(fake_sleep_seconds=1900, should_fail=False)
        vp = {"id": "VP-001", "verification_method": "automated_test"}

        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(backend.execute(vp, timeout_seconds=1800))

    def test_fake_backend_timeout_uses_caller_timeout(self):
        """A small caller-timeout still triggers the timeout path.

        Sanity check that the fake backend does not have a hidden
        minimum timeout — a 0.05s caller-timeout with the default 1s
        sleep must also raise.
        """
        backend = FakeVerifierBackend()  # default fake_sleep_seconds=1.0
        vp = {"id": "VP-001", "verification_method": "automated_test"}

        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(backend.execute(vp, timeout_seconds=0.05))


# -----------------------------------------------------------------------------
# Factory: _select_backend() chooses real vs fake
# -----------------------------------------------------------------------------


class TestSelectBackendFactory:
    """The env-var-controlled factory must default to the real backend."""

    def test_select_backend_returns_real_when_env_not_set(self, monkeypatch):
        """No ``VERIFICATION_PROFILE`` env var → real backend (no fake).

        Boundary condition: callers that never set the env var must
        see the production backend, otherwise a fresh checkout would
        silently start using the fake.
        """
        monkeypatch.delenv("VERIFICATION_PROFILE", raising=False)

        backend = _select_backend()

        assert not isinstance(backend, FakeVerifierBackend)

    def test_select_backend_returns_fake_when_dry_run(self, monkeypatch):
        """``VERIFICATION_PROFILE=dry_run`` → ``FakeVerifierBackend``."""
        monkeypatch.setenv("VERIFICATION_PROFILE", "dry_run")

        backend = _select_backend()

        assert isinstance(backend, FakeVerifierBackend)

    def test_select_backend_other_profile_values_return_real(self, monkeypatch):
        """Non-``dry_run`` profile values must not select the fake backend.

        Forward-compatibility: a future ``smoke`` or ``ci`` profile
        should also bypass the fake unless explicitly opted in.
        """
        for profile in ("ci", "smoke", "production", "", "DRY_RUN"):
            monkeypatch.setenv("VERIFICATION_PROFILE", profile)
            backend = _select_backend()
            assert not isinstance(backend, FakeVerifierBackend), (
                f"profile={profile!r} unexpectedly returned the fake backend"
            )
