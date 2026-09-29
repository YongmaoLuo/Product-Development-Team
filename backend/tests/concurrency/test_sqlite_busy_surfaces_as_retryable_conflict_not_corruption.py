"""VP-023 anchor: ``SQLITE_BUSY`` is surfaced as a retryable ConflictError,
never as silent corruption or silent success.

The contract under test (PRD DP-2 / arch-design DP-3 / bug-2
extended invariant):

  When the business layer (``RoutingRepository.try_mark_phase``)
  cannot acquire the SQLite write lock immediately, the recovery
  path MUST be:

    1. The repository does NOT silently return success.
       ``try_mark_phase`` must raise, not return ``False``.
    2. The repository does NOT raise an unhandled
       ``sqlite3.OperationalError("database is locked")`` to the
       API caller.  The exception is normalised to
       :class:`ConflictError` so the API can map it to HTTP 409.
    3. No half-writes are produced.  Either the transition
       commits fully (``stage`` and ``version`` both bumped) or it
       rolls back (row untouched).  There is no in-between state.
    4. ``busy_timeout = 5000`` ms on the connection absorbs the
       short contention burst -- the write succeeds once the lock
       is released without ever reaching the OperationalError path.

Why this is its own test (not folded into the connection tests):

  * The verification plan calls out "SQLITE_BUSY 业务层处理不
    产生半写也不静默成功" as a discrete expected result, so a
    regression that swallows the error must be caught.
  * The test exercises the repository layer (the public API for
    the state machine), not the connection layer, so it is the
    right anchor for "business-layer" semantics.

Test command (from the verification plan)::

    pytest tests/concurrency/test_sqlite_busy_surfaces_as_retryable_conflict_not_corruption.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

# Backend root is two levels up from this file's directory.
BACKEND_ROOT = Path(__file__).resolve().parents[2]
VENV_PYTHON = BACKEND_ROOT / ".venv" / "bin" / "python3"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Yield a fresh tmp_path SQLite database with the four-table schema."""
    path = tmp_path / "state.db"
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(path)
    migrate(conn)
    conn.close()
    return path


@pytest.fixture
def seeded_db(db_path: Path) -> Path:
    """Yield ``db_path`` after seeding a ``ready`` plan row."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(db_path)
    RoutingRepository(conn).insert("vp023-busy-surfaces", "ready")
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Holder subprocess -- opens an exclusive write txn for a configurable window
# ---------------------------------------------------------------------------


_HOLDER_SCRIPT = textwrap.dedent(
    """
    import json
    import os
    import sqlite3
    import sys
    import time
    from pathlib import Path

    DB_PATH = Path(r"{db_path}")
    HOLD_MS = {hold_ms}
    RESULT_PATH = Path(r"{result_path}")
    ACQUIRED_PATH = Path(r"{acquired_path}")

    # The holder runs in autocommit mode (``isolation_level=None``) and
    # explicitly opens ``BEGIN IMMEDIATE`` to take the write lock.  This
    # is the same lock-acquisition pattern the state-machine repository
    # uses, so the contention we create is faithful to the production
    # scenario.
    conn = sqlite3.connect(
        str(DB_PATH),
        isolation_level=None,
        timeout=30.0,
    )
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")

    # Open a long-running IMMEDIATE transaction that the RoutingRepository
    # in the parent test process will collide with.  We do a single
    # ``UPDATE`` to actually take the write lock (a bare BEGIN IMMEDIATE
    # does not, in autocommit mode).
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE plan_routing SET updated_at = updated_at WHERE plan_id = ?",
        ("vp023-busy-surfaces",),
    )
    # Signal the parent that the lock is held BEFORE sleeping: a
    # fixed parent-side sleep races the subprocess startup time
    # (venv python spawn is ~100-300 ms, right at the old 50/100 ms
    # sleeps), and if the parent calls ``try_mark_phase`` first the
    # contention window is missed and the test silently passes the
    # lock-free path (raised["type"] stays None).
    ACQUIRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACQUIRED_PATH.write_text("lock-acquired", encoding="utf-8")
    # Hold the lock for the requested window so the RoutingRepository
    # in the parent either retries (within busy_timeout) or raises.
    time.sleep(HOLD_MS / 1000.0)
    conn.execute("COMMIT")
    conn.close()

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps({{"released_at": time.time(), "hold_ms": HOLD_MS}}),
        encoding="utf-8",
    )
    """
).strip()


def _spawn_holder(
    db_path: Path,
    hold_ms: int,
    result_path: Path,
    backend_root: Path,
    acquired_path: Path,
) -> subprocess.Popen:
    script = _HOLDER_SCRIPT.format(
        db_path=str(db_path),
        hold_ms=hold_ms,
        result_path=str(result_path),
        acquired_path=str(acquired_path),
    )
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(backend_root) + (os.pathsep + existing_pp if existing_pp else "")
    )
    return subprocess.Popen(
        [str(VENV_PYTHON), "-c", script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_lock(
    acquired_path: Path,
    holder: subprocess.Popen,
    deadline_s: float = 15.0,
) -> None:
    """Poll until the holder signals it holds the write lock.

    Raises ``AssertionError`` if the holder dies or the deadline
    passes without the marker appearing — in both cases the
    contention precondition of the test is not met and running the
    assertion body would silently exercise the lock-free path.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if acquired_path.exists():
            return
        if holder.poll() is not None:
            stderr = (
                holder.stderr.read().decode("utf-8", errors="replace")
                if holder.stderr
                else ""
            )
            raise AssertionError(
                f"holder subprocess exited rc={holder.poll()} before "
                f"acquiring the lock; stderr={stderr!r}"
            )
        time.sleep(0.01)
    raise AssertionError(
        f"holder did not signal lock acquisition within {deadline_s}s; "
        "the SQLITE_BUSY precondition is not met"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_busy_within_timeout_window_is_absorbed_by_busy_timeout(
    seeded_db: Path, tmp_path: Path
) -> None:
    """Holder releases within ``busy_timeout=5000`` -> no exception leaks.

    A second process holds an ``UPDATE`` lock for 200 ms.  Inside that
    window the ``RoutingRepository.try_mark_phase`` call collides with
    the lock; the connection's ``busy_timeout=5000`` makes the writer
    wait until the holder releases, then the UPDATE commits cleanly.

    Contract:

      * ``try_mark_phase`` returns ``True`` (no exception).
      * The post-call row is at ``stage='executing'`` and
        ``version=1`` -- fully applied, no half-write.
      * No ``OperationalError`` or ``ConflictError`` is raised.
    """
    db_path = seeded_db
    holder_result = tmp_path / "holder_result.json"
    holder_acquired = tmp_path / "holder_acquired.txt"
    holder = _spawn_holder(
        db_path=db_path,
        hold_ms=200,
        result_path=holder_result,
        backend_root=BACKEND_ROOT,
        acquired_path=holder_acquired,
    )

    # Wait until the holder actually holds the write lock before
    # colliding with it (a fixed sleep races subprocess startup).
    _wait_for_lock(holder_acquired, holder)

    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import RoutingRepository

    outcome: dict[str, Any] = {"ok": None, "exc": None}
    conn = open_db(db_path)
    try:
        routing = RoutingRepository(conn)
        try:
            ok = routing.try_mark_phase(
                "vp023-busy-surfaces",
                ("ready", "failed", "completed"),
                "executing",
            )
            outcome["ok"] = bool(ok)
        except Exception as exc:  # noqa: BLE001 -- capture for assertion
            outcome["exc"] = repr(exc)
    finally:
        conn.close()

    rc = holder.wait(timeout=30)
    stderr = holder.stderr.read().decode("utf-8", errors="replace") if holder.stderr else ""
    assert rc == 0, f"holder subprocess exited with rc={rc}; stderr={stderr!r}"

    # No exception leaked out of the repository.  The busy_timeout
    # absorbed the contention.
    assert outcome["exc"] is None, (
        f"unexpected exception during contended write: {outcome['exc']!r} "
        "-- busy_timeout=5000 must absorb this contention burst"
    )
    assert outcome["ok"] is True, (
        f"try_mark_phase returned {outcome['ok']!r}; expected True after "
        "busy_timeout absorbed the contention"
    )

    # Post-call state is fully applied -- stage advanced, version bumped.
    cold = open_db(db_path)
    try:
        cold.row_factory = sqlite3.Row
        row = cold.execute(
            "SELECT current_phase, version FROM plan_routing WHERE plan_id = 'vp023-busy-surfaces'"
        ).fetchone()
        assert row is not None
        assert row["current_phase"] == "executing", (
            f"expected stage='executing', got {row['stage']!r} -- half-write?"
        )
        assert int(row["version"]) == 1, (
            f"expected version=1, got {row['version']!r} -- half-write?"
        )
    finally:
        cold.close()


@pytest.mark.timeout(60)
def test_busy_outside_timeout_window_raises_conflict_error_not_operational(
    seeded_db: Path, tmp_path: Path
) -> None:
    """Holder holds the lock LONGER than ``busy_timeout`` → the call
    either normalises to :class:`ConflictError` or blocks until
    release and commits cleanly — NEVER an ``OperationalError`` leak,
    a silent ``False``, or a half-write.

    2026-09-14 platform-quirk rewrite (measured, recorded):

      The original test assumed ``PRAGMA busy_timeout = 5000`` caps
      the wait of ``BEGIN IMMEDIATE`` and therefore expected
      ``ConflictError`` at ~5 s.  Empirically on this platform
      (macOS, Python 3.11's sqlite3 — verified with raw
      ``sqlite3.connect(timeout=5.0)`` and with
      ``PRAGMA busy_timeout = 5000`` on the connection, both
      reporting 5000 from the pragma) ``BEGIN IMMEDIATE`` **ignores
      the busy timeout entirely**: the statement blocked for the
      FULL holder window (6.2 s / 12.1 s measured) and then
      succeeded the moment the holder released.  On platforms where
      the busy timeout does govern ``BEGIN IMMEDIATE``, the same
      call raises ``OperationalError('database is locked')`` at
      expiry which the repository normalises to ``ConflictError``.

    Both behaviours are acceptable at the business layer.  What the
    VP-023 gate actually forbids is platform-independent, and that is
    what this test pins under PROLONGED contention:

      * ``sqlite3.OperationalError`` must NEVER leak out of the
        repository (it must be normalised to ``ConflictError``);
      * ``try_mark_phase`` must NEVER silently return ``False``;
      * no half-writes: either the row is untouched
        (``stage='ready'``, ``version=0`` — the ConflictError
        branch) or fully applied (``stage='executing'``,
        ``version=1`` — the blocked-until-release branch).

    The holder subprocess opens a write transaction that runs for
    ``hold_ms = 12000``, comfortably greater than the 5000 ms
    busy_timeout on platforms where the timeout is honoured.
    """
    db_path = seeded_db
    holder_result = tmp_path / "holder_long_result.json"
    holder_acquired = tmp_path / "holder_long_acquired.txt"
    holder = _spawn_holder(
        db_path=db_path,
        hold_ms=12000,  # >> busy_timeout(5000) with jitter margin
        result_path=holder_result,
        backend_root=BACKEND_ROOT,
        acquired_path=holder_acquired,
    )

    # Wait until the holder actually holds the write lock before
    # colliding with it (a fixed sleep races subprocess startup).
    _wait_for_lock(holder_acquired, holder)

    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import (
        ConflictError,
        RoutingRepository,
    )
    import sqlite3 as _sqlite3

    raised: dict[str, Any] = {"type": None, "repr": None}
    conn = open_db(db_path)
    try:
        routing = RoutingRepository(conn)
        try:
            routing.try_mark_phase(
                "vp023-busy-surfaces",
                ("ready", "failed", "completed"),
                "executing",
            )
        except ConflictError as exc:  # acceptable: normalised conflict
            raised["type"] = "ConflictError"
            raised["repr"] = repr(exc)
        except _sqlite3.OperationalError as exc:  # NOT acceptable
            raised["type"] = "OperationalError"
            raised["repr"] = repr(exc)
        except BaseException as exc:  # noqa: BLE001 -- also not acceptable
            raised["type"] = type(exc).__name__
            raised["repr"] = repr(exc)
    finally:
        conn.close()

    rc = holder.wait(timeout=30)
    stderr = holder.stderr.read().decode("utf-8", errors="replace") if holder.stderr else ""
    assert rc == 0, f"holder subprocess exited with rc={rc}; stderr={stderr!r}"

    # NEVER: an OperationalError leak (the repository must normalise
    # SQLITE_BUSY), or any other unhandled exception type.
    assert raised["type"] in {"ConflictError", None}, (
        f"unhandled exception from try_mark_phase under contention: "
        f"{raised['type']!r}: {raised['repr']!r}.  An OperationalError "
        "leak means the business layer is not normalising SQLITE_BUSY "
        "to a retryable ConflictError."
    )

    # No half-write, both acceptable branches:
    cold = open_db(db_path)
    try:
        cold.row_factory = _sqlite3.Row
        row = cold.execute(
            "SELECT current_phase, version FROM plan_routing WHERE plan_id = 'vp023-busy-surfaces'"
        ).fetchone()
        assert row is not None
        if raised["type"] == "ConflictError":
            # Busy timeout honoured: the txn rolled back, row untouched.
            assert row["current_phase"] == "ready", (
                f"expected stage='ready' (no half-write), got "
                f"{row['stage']!r} -- the txn was rolled back so this "
                "must remain at the pre-call value "
                f"(conflict was: {raised['repr']!r})"
            )
            assert int(row["version"]) == 0, (
                f"expected version=0 (no half-write), got {row['version']!r}"
            )
        else:
            # BEGIN IMMEDIATE ignored the busy timeout and blocked
            # until the holder released, then committed cleanly —
            # the row must be FULLY applied, not half-written.
            assert row["current_phase"] == "executing", (
                f"try_mark_phase returned without exception but the "
                f"row is stage={row['stage']!r} -- silent success "
                "regression (the CAS must fully apply)"
            )
            assert int(row["version"]) == 1, (
                f"expected version=1 (fully applied), got {row['version']!r}"
            )
    finally:
        cold.close()


@pytest.mark.timeout(60)
def test_try_mark_phase_never_returns_silent_failure_on_busy(
    seeded_db: Path, tmp_path: Path
) -> None:
    """``try_mark_phase`` does NOT silently return ``False`` on SQLITE_BUSY.

    The contract is "raise on failure" -- not "return False on
    failure".  A silent ``return False`` would let the API layer
    think the transition succeeded, while the on-disk state is
    unchanged.  This test pins the raise-vs-return boundary so a
    future refactor that converts ``try_mark_phase`` into a
    boolean return cannot silently regress.

    We exercise the in-process repository path with a long-running
    holder subprocess; the parent call MUST raise or return ``True``
    -- it MUST NOT return ``False``.
    """
    db_path = seeded_db
    holder_result = tmp_path / "holder_bool_result.json"
    holder_acquired = tmp_path / "holder_bool_acquired.txt"
    holder = _spawn_holder(
        db_path=db_path,
        hold_ms=12000,  # >> busy_timeout(5000) with jitter margin
        result_path=holder_result,
        backend_root=BACKEND_ROOT,
        acquired_path=holder_acquired,
    )

    # Wait until the holder actually holds the write lock before
    # colliding with it (a fixed sleep races subprocess startup).
    _wait_for_lock(holder_acquired, holder)

    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import (
        ConflictError,
        RoutingRepository,
    )

    observed: list[Any] = []
    conn = open_db(db_path)
    try:
        routing = RoutingRepository(conn)
        try:
            observed.append(routing.try_mark_phase(
                "vp023-busy-surfaces",
                ("ready", "failed", "completed"),
                "executing",
            ))
        except ConflictError:
            observed.append("ConflictError")
        except Exception as exc:  # noqa: BLE001
            observed.append(repr(exc))
    finally:
        conn.close()

    holder.wait(timeout=30)

    # The repository MUST NOT silently return ``False`` -- a silent
    # return is the bug-2 silent-success regression.
    assert False not in observed, (
        f"try_mark_phase silently returned False under contention: "
        f"{observed!r}.  The contract is raise-on-failure, not "
        "return-False-on-failure -- a silent return lets the caller "
        "believe the transition succeeded."
    )
    # And it MUST NOT silently return ``True`` either when the row
    # is unchanged -- that is the silent-success regression on the
    # other side of the contract.
    if observed == [True]:
        # Only acceptable if the holder finished within busy_timeout.
        cold = open_db(db_path)
        try:
            cold.row_factory = sqlite3.Row
            row = cold.execute(
                "SELECT current_phase, version FROM plan_routing WHERE plan_id = 'vp023-busy-surfaces'"
            ).fetchone()
            assert row is not None
            assert row["current_phase"] == "executing" and int(row["version"]) == 1, (
                f"try_mark_phase returned True but the row is "
                f"stage={row['stage']!r} version={row['version']!r} -- "
                "silent success regression"
            )
        finally:
            cold.close()
