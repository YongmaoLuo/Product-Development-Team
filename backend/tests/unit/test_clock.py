"""TDD spec for :mod:`framework.clock` (architecture decision point 5).

Covers the 5 contract points:

1. ``utcnow_iso()`` returns ``str``;
2. the value round-trips through ``datetime.fromisoformat()``;
3. the parsed value is tz-aware with ``utcoffset() == timedelta(0)``;
4. two consecutive calls satisfy ``t1 <= t2`` both lexicographically and
   chronologically (zero-padded, fixed-width format);
5. freezing the underlying clock via monkeypatch makes the return value
   stable — i.e. the function is injectable.
"""

from datetime import datetime, timedelta, timezone

import pytest

from framework import clock
from framework.clock import utcnow_iso

pytestmark = pytest.mark.unit


def test_returns_str():
    assert isinstance(utcnow_iso(), str)


def test_parses_back_with_fromisoformat():
    parsed = datetime.fromisoformat(utcnow_iso())
    assert isinstance(parsed, datetime)


def test_parsed_value_is_utc_aware():
    parsed = datetime.fromisoformat(utcnow_iso())
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_monotonic_string_and_time_order():
    t1 = utcnow_iso()
    t2 = utcnow_iso()
    # Lexicographic order must match chronological order (zero padding).
    assert t1 <= t2
    assert datetime.fromisoformat(t1) <= datetime.fromisoformat(t2)


def test_fixed_width_microseconds():
    """Fixed-width fractional part keeps string order == time order."""
    value = utcnow_iso()
    assert value.endswith("+00:00")
    # ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00`` -> 32 chars.
    assert len(value) == 32, value
    assert value[19] == "."


def test_frozen_clock_is_stable(monkeypatch):
    """Injectability: freezing the module-level datetime pins the output."""
    frozen = datetime(2026, 8, 7, 12, 34, 56, 789012, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(clock, "datetime", _FrozenDatetime)

    first = clock.utcnow_iso()
    second = clock.utcnow_iso()
    assert first == second == "2026-08-07T12:34:56.789012+00:00"
