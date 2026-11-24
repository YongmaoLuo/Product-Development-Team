"""backend/plan_scheduler.retry — one bounded backoff for a locked database.

Background
----------
The scheduler writes to SQLite from several places, and every one of
them can be holding the write lock when the next one arrives.  The
connection layer already sets ``busy_timeout = 5000`` ms, which
absorbs a *short* contention burst — but a write transaction that
outlives the timeout surfaces as ``OperationalError: database is
locked`` and the caller sees an error that has nothing to do with the
thing it was trying to do.

Until now each call site caught that error on its own, so each one
grew its own backoff, and none of the backoffs was bounded by
anything shared: one of them slept half a second, another gave up at
the first lock, another retried forever.  The same contention
therefore cost different amounts of time — and risk — depending on
which writer lost the race.

What this module offers
-----------------------
:func:`retry_on_busy` is the **one** way a scheduler writer waits out
a lock.  It is a pure function of the callable it wraps:

* **Only locks are retried.**  A missing table is not going to be
  fixed by waiting, so every other error is re-raised at once, with
  the attempt count untouched.
* **The backoff is exponential and bounded.**  The delay doubles
  after each attempt until it reaches ``max_delay`` and then stays
  there.  An uncapped backoff turns a long lock into a hang; a
  backoff that does not grow hammers a database that is already busy.
* **The number of attempts is bounded too.**  When they are spent the
  *last* lock is raised, with the traceback it was raised with — the
  caller still sees where the lock happened, which a wrapper
  exception would have hidden.
* **The sleep function is an argument.**  A test passes a recorder and
  never really sleeps; production passes :func:`time.sleep`.

Nothing here is wired to a call site yet — the writers adopt it in the
tasks that follow — so what this module fixes is the *contract*, not
the behaviour of any particular writer.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

#: The phrase SQLite puts in the message of a busy database.  Matched
#: case-insensitively, because the wording differs slightly between
#: SQLite builds ("database is locked" vs "database table is locked"
#: is a different phrase and is *not* this one).
_LOCKED_PHRASE = "database is locked"


def is_db_locked(exc: BaseException) -> bool:
    """Whether ``exc`` is SQLite's "database is locked".

    Only :class:`sqlite3.OperationalError` counts, and only when its
    message carries the locked phrase.  Everything else — a missing
    table, a disk error, an exception from another layer that happens
    to mention a lock in its text — is not a lock, and is not retried.

    Args:
        exc: The exception to classify.

    Returns:
        ``True`` when the exception is SQLite reporting a busy
        database, ``False`` for every other exception.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    return _LOCKED_PHRASE in str(exc).lower()


def retry_on_busy(
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 0.05,
    max_delay: float = 0.4,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn``, retrying it while SQLite says the database is locked.

    The callable is invoked at least once.  While it raises
    ``database is locked`` the caller waits — ``base_delay`` seconds,
    doubling after each attempt, never more than ``max_delay`` — and
    calls it again, up to ``attempts`` times in total.  An error that
    is not a lock is raised at once, without a second attempt and
    without a wait.

    Args:
        fn: The zero-argument callable to run.
        attempts: How many times ``fn`` may be called in total.  A
            value below one is treated as one: the callable is always
            called at least once.
        base_delay: The first wait, in seconds.  It doubles after each
            attempt.
        max_delay: The ceiling on any single wait, in seconds.
        sleep: How to wait.  Injectable so that a test never sleeps.

    Returns:
        Whatever ``fn`` returns.

    Raises:
        Exception: The last lock error, with its original traceback,
            once the attempts are spent; or the first non-lock error,
            raised as it happened.
    """
    attempts = max(1, int(attempts))
    ceiling = max(0.0, float(max_delay))
    delay = max(0.0, float(base_delay))

    last: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not is_db_locked(exc):
                raise
            last = exc
            if attempt + 1 >= attempts:
                break
            sleep(min(delay, ceiling))
            delay = min(delay * 2, ceiling)

    if last is None:  # pragma: no cover - the loop always calls fn once
        raise RuntimeError("retry_on_busy: the callable was never invoked")
    # The last lock, with the traceback it was raised with: the caller
    # has to be able to see *where* the database was busy.
    raise last