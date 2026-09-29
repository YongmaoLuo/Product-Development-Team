"""SQLite connection helper for the state-machine WAL base layer.

The :func:`open` factory produces a :class:`sqlite3.Connection` whose
PRAGMAs are pinned to the values the state-machine refactor assumes
(WAL journal mode, ``busy_timeout = 5000`` ms, ``synchronous = NORMAL``,
and ``isolation_level = None`` for autocommit-style control).

These four PRAGMAs are the contract that the rest of the state-machine
codebase depends on:
  * WAL keeps readers non-blocking under concurrent writers (the
    planner / executor / verification threads may all touch the DB).
  * ``busy_timeout`` saves us from ``OperationalError: database is
    locked`` on short contention bursts.
  * ``synchronous = NORMAL`` is the canonical SQLite / WAL tradeoff
    (durable on commit, but tolerates a power loss between checkpoints).
  * ``isolation_level = None`` lets callers decide when to commit
    explicitly; the migration script relies on this so its DDL
    applies instantly without an implicit transaction.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from config_paths import STATE_DB
from typing import Union

__all__ = ["open", "REAL_STATE_DB", "ALLOW_REAL_DB_ENV"]

DbPath = Union[str, Path]

#: The operator's live database, declared once in :mod:`config_paths`
#: rather than re-derived from ``__file__`` here. This module sits four
#: levels deep, so a wrong ``parents[N]`` still resolves to a *valid*
#: path — it just points somewhere no other process ever reads. That is
#: the 2026-09-12 failure (see
#: ``verification.orchestrator._state_db_path_for_orchestrator``), and
#: it is why the path is not reconstructed locally.
REAL_STATE_DB: Path = STATE_DB

#: Escape hatch for a deliberate one-off audit that genuinely needs the
#: live database. Never set this in CI or in a normal test run.
ALLOW_REAL_DB_ENV = "PDT_ALLOW_REAL_STATE_DB"


def _refuse_real_state_db_under_pytest(db_path: DbPath) -> None:
    """Hard-block a test run from opening the operator's live ``state.db``.

    2026-09-13: ``backend/tests/perf/test_repo_scheduler_refiner_watchdog.py``
    resolved its database as ``PDT_STATE_DB_PATH`` *or, failing that,* the
    repository's own ``state.db``. The env var is set by a conftest, so the
    fallback looked inert — but any invocation that did not collect that
    conftest wrote straight to production: 200 ``plan_tasks`` rows landed in
    the operator's database, and the row's plan id (a pytest ``tmp_path``
    directory name) then surfaced in the plan list as if it were real.

    The lesson is that a per-tree conftest cannot be the last line of
    defence: this repository has four test trees with four different
    rootdirs, and a fifth can always appear. The one chokepoint every
    reader and writer already shares is this factory, so the guard lives
    here and covers them all.

    Skipped outside pytest (the backend server and the executor open this same
    database legitimately) and when the operator opts in explicitly.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return  # not a test run — the server / executor / CLI
    if os.environ.get(ALLOW_REAL_DB_ENV):
        return  # deliberate, operator-approved one-off audit
    try:
        resolved = Path(db_path).resolve()
    except OSError:
        return
    if resolved != REAL_STATE_DB:
        return
    raise RuntimeError(
        f"refusing to open the live state.db ({REAL_STATE_DB}) from a test. "
        f"Point PDT_STATE_DB_PATH at a throwaway file instead — a test must "
        f"never read or write the operator's database. Set "
        f"{ALLOW_REAL_DB_ENV}=1 only for a deliberate one-off audit."
    )


def open(db_path: DbPath) -> sqlite3.Connection:
    """Open (or create) a SQLite database at ``db_path`` with the WAL contract.

    The four PRAGMAs below are applied after the connection is opened
    so callers see them on the returned handle immediately:

        PRAGMA journal_mode = WAL
        PRAGMA busy_timeout = 5000
        PRAGMA synchronous = NORMAL
        isolation_level = None

    Parameters
    ----------
    db_path:
        Path to the SQLite database file. May be a :class:`str` or
        :class:`pathlib.Path`. The parent directory is created if it
        does not exist.

    Raises
    ------
    ValueError
        If ``db_path`` is empty (treated as a programmer error — an
        empty path means the caller forgot to compute the path).
    PermissionError
        If the file cannot be created/opened because the OS denies
        write access (e.g. read-only parent directory).
    """
    if db_path is None:
        raise ValueError("db_path must not be None")
    path_str = str(db_path)
    if not path_str:
        raise ValueError("db_path must not be empty")

    _refuse_real_state_db_under_pytest(db_path)

    path = Path(db_path)
    # ``sqlite3.connect`` creates the file if it does not exist, but
    # it does NOT create the parent directory.  Create the parent
    # eagerly so callers can use ``open(DATA_DIR / "state.db")`` against
    # a freshly-created data directory.
    if path.parent and not path.parent.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            # Re-raise: the caller needs to know the data directory
            # is read-only, not silently fall back to a different path.
            raise

    # ``isolation_level = None`` puts the connection in autocommit
    # mode; the migration script and the future repository methods
    # rely on this so transaction boundaries are explicit.
    #
    # ``check_same_thread = False`` lets the same connection be used
    # from worker threads (the dispatcher may persist from a worker
    # thread pool). SQLite's default of ``True`` would raise
    # ``ProgrammingError: SQLite objects created in a thread can only
    # be used in that same thread`` on every cross-thread persist;
    # turning the check off is safe because we serialise writes
    # through ``BEGIN IMMEDIATE`` (a per-call mutex on the repository
    # side), so two threads cannot interleave on the same connection.
    try:
        conn = sqlite3.connect(
            path_str,
            isolation_level=None,
            timeout=30.0,
            check_same_thread=False,
        )
    except sqlite3.OperationalError as exc:
        # SQLite surfaces "unable to open database file" when the
        # parent directory is not writable.  The upstream task spec
        # pins this case to ``PermissionError`` so repository helpers
        # can catch a single exception type — translate here.
        msg = str(exc).lower()
        if "unable to open" in msg or "permission" in msg or "read-only" in msg:
            raise PermissionError(
                f"cannot open SQLite database at {path_str!r}: {exc}"
            ) from exc
        raise

    # Apply the four pinned PRAGMAs. Each one is a separate
    # ``executescript``/``execute`` call so the PRAGMA value is
    # returned through the row factory and we can both commit the
    # setting and verify it server-side.
    #
    # 1) WAL journal mode. SQLite returns the *new* mode; if it
    #    cannot be set (e.g. on a read-only filesystem) it returns
    #    the current mode — but that is propagated to the caller
    #    via the PRAGMA query, not through this helper.
    conn.execute("PRAGMA journal_mode = WAL")
    # 2) 5-second busy timeout.
    conn.execute("PRAGMA busy_timeout = 5000")
    # 3) Synchronous = NORMAL (1). SQLite accepts the spelled-out
    #    ``NORMAL`` token as well as the integer 1.
    conn.execute("PRAGMA synchronous = NORMAL")

    # The ``isolation_level`` attribute is a Connection-level property,
    # not a PRAGMA.  Setting it post-hoc is permitted, but we already
    # passed it via ``sqlite3.connect`` — assert here so a future
    # refactor that drops the kwarg is caught at import time.
    assert conn.isolation_level is None, (
        "open() must produce a connection in autocommit mode "
        "(isolation_level=None); the state-machine repository layer "
        "depends on this contract for explicit transaction control."
    )

    return conn
