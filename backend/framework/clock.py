"""Single UTC timestamp injection point for the backend.

Architecture decision point 5 mandates that *every* timestamp written by
the framework (scheduler ``schedule_ts``, worker ``end_ts``, ...) flows
through this one function. Modules MUST NOT call ``datetime.now()``
locally — doing so reintroduces naive/local-timezone drift and makes the
clock un-mockable in tests.

Usage::

    from framework.clock import utcnow_iso

    schedule_ts = utcnow_iso()   # '2026-08-07T12:34:56.789012+00:00'
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utcnow_iso"]


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    The returned string:

    * is always ``str``;
    * round-trips through :meth:`datetime.datetime.fromisoformat`;
    * always carries an explicit UTC offset (``+00:00``) — never naive;
    * is zero-padded in every field, so lexicographic ordering of two
      returned values matches chronological ordering.

    Tests freeze the clock by monkeypatching ``framework.clock.datetime``.
    """
    # ``timespec="microseconds"`` guarantees a fixed-width fractional part
    # (``.000000`` rather than an omitted field when microsecond == 0), so
    # lexicographic string comparison stays consistent with time order.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
