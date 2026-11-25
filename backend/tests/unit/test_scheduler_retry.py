"""One bounded backoff for a locked database.

The scheduler writes to SQLite from several places — the queue, the
slot ledger, the decision log — and every one of them can be holding
the write lock when the next one arrives.  SQLite's own
``busy_timeout`` (``5000`` ms) absorbs a *short* contention burst, but
a write transaction that outlives it surfaces as
``OperationalError: database is locked`` and the caller sees an error
that has nothing to do with what it was trying to do.

Each call site used to catch that error on its own, which means each
one grew its own backoff, and none of them was bounded by anything
shared.  This module pins the shared one: :mod:`plan_scheduler.retry`
offers a **pure** retryer that

* retries **only** ``database is locked`` — every other error is
  re-raised at once, because a missing table is not going to be fixed
  by waiting;
* backs off **exponentially** and **boundedly** — the delay grows, it
  is capped, and the number of attempts is capped;
* **gives up** by raising the *last* lock error with the traceback it
  was raised with, so the caller still sees where the lock happened;
* takes the sleep function as an argument, so a test never really
  sleeps.

Nothing calls it yet — the call sites are wired up by the tasks that
follow — so what is pinned here is the contract, not the behaviour of
any particular writer.
"""

from __future__ import annotations

import sqlite3
import traceback

import pytest

from plan_scheduler.retry import is_db_locked, retry_on_busy


# ---------------------------------------------------------------------------
# The four contracts
# ---------------------------------------------------------------------------


def test_retries_until_success() -> None:
    """Two locks, then the call works: three calls, one result.

    The retryer is not allowed to give up on a lock that the next
    attempt would have survived — a lock is a *transient* condition,
    and the whole point of retrying it is that it goes away.
    """
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    result = retry_on_busy(flaky, attempts=3, base_delay=0.01, sleep=lambda s: None)

    assert result == "ok"
    assert calls["n"] == 3, "the callable must be called once per attempt"


def test_non_locked_raises_immediately() -> None:
    """A missing table is not a lock: one call, the error comes straight out.

    Retrying anything that is not ``database is locked`` would turn a
    programming error (a typo in a table name) into a slow, silent
    retry loop that still fails.
    """
    calls = {"n": 0}

    def missing_table() -> str:
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: scheduler_slot")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        retry_on_busy(missing_table, attempts=4, base_delay=0.01, sleep=lambda s: None)

    assert calls["n"] == 1, "a non-lock error must not be retried"


def test_exhausted_reraises_last() -> None:
    """Attempts spent: the last lock is raised, with its traceback intact.

    The caller has to be able to see *where* the lock happened, so the
    exception that comes out is the one the callable raised — not a
    wrapper around it, and not a lock error without a traceback.
    """
    calls = {"n": 0}

    def always_locked() -> str:
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked (last attempt)")

    with pytest.raises(sqlite3.OperationalError) as excinfo:
        retry_on_busy(always_locked, attempts=3, base_delay=0.01, sleep=lambda s: None)

    assert calls["n"] == 3, "the callable must be called exactly `attempts` times"
    assert "last attempt" in str(excinfo.value)

    frames = [frame.name for frame in traceback.extract_tb(excinfo.value.__traceback__)]
    assert "always_locked" in frames, (
        "the original traceback must survive: the frame that raised the lock "
        f"has to still be in it, got {frames!r}"
    )


def test_delay_is_capped() -> None:
    """The backoff grows, and it never grows past ``max_delay``.

    An uncapped backoff turns a long lock into a hang; a backoff that
    does not grow at all hammers a database that is already busy.  The
    delay therefore doubles until it reaches the cap and then stays
    there.
    """
    slept: list[float] = []

    def always_locked() -> str:
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        retry_on_busy(
            always_locked,
            attempts=6,
            base_delay=0.05,
            max_delay=0.4,
            sleep=slept.append,
        )

    assert len(slept) == 5, "one sleep between each pair of attempts, none after the last"
    assert all(delay <= 0.4 for delay in slept), f"a delay exceeded the cap: {slept!r}"
    assert slept[:3] == pytest.approx([0.05, 0.1, 0.2]), (
        f"the delay has to grow exponentially: {slept!r}"
    )
    assert slept[3] == pytest.approx(0.4), (
        f"the delay has to stop at the cap instead of passing it: {slept!r}"
    )
    assert slept[4] == pytest.approx(0.4), (
        f"the delay has to stay at the cap: {slept!r}"
    )


# ---------------------------------------------------------------------------
# The edges around the four contracts
# ---------------------------------------------------------------------------


def test_is_db_locked_only_for_the_lock() -> None:
    """The predicate names one error and no other."""
    assert is_db_locked(sqlite3.OperationalError("database is locked")) is True
    assert is_db_locked(sqlite3.OperationalError("DATABASE IS LOCKED")) is True
    assert is_db_locked(sqlite3.OperationalError("no such table: x")) is False
    assert is_db_locked(sqlite3.OperationalError("disk I/O error")) is False
    assert is_db_locked(ValueError("database is locked")) is False


def test_attempts_below_one_is_treated_as_one() -> None:
    """``attempts=0`` means "call it once" — not "never call it".

    A retryer that never calls the callable would report a lock that
    never happened, and one that calls it no times at all cannot raise
    the last error either.
    """
    calls = {"n": 0}

    def always_locked() -> str:
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        retry_on_busy(always_locked, attempts=0, base_delay=0.01, sleep=lambda s: None)

    assert calls["n"] == 1