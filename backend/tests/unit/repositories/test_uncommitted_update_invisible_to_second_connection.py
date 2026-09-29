"""Uncommitted update must be invisible to a second connection (VP-014).

Bug-class anchor (VP-014): SQLite's transaction model guarantees that
a row UPDATEd inside an uncommitted transaction is invisible to every
OTHER connection in the same database.  Two independent
:func:`state_machine.db.connection.open` calls each get their own
SQLite handle, their own implicit read transaction, and their own
``BEGIN IMMEDIATE`` / ``COMMIT`` lifecycle.

This test pins down the reader side of that contract: while a
*writer* connection holds an open transaction with a pending
``UPDATE``, an independent *reader* connection MUST continue to see
the pre-update value (the old committed snapshot) and MUST NOT be
blocked by the writer.

Specifically:

  1. Open ``A`` and ``B`` against the same DB file (the WAL contract
     is established by :func:`state_machine.db.connection.open`).
  2. Seed a row in ``plan_routing`` (committed) on a bootstrap
     connection.
  3. On connection ``A``:
       * ``BEGIN IMMEDIATE``
       * ``UPDATE plan_routing SET stage = 'dirty' WHERE plan_id = ?``
     and do NOT commit yet.
  4. On connection ``B``:
       * ``SELECT current_phase FROM plan_routing WHERE plan_id = ?``
     MUST return the **old** committed value (``'clean'``) - not
     ``'dirty'``.  This is the uncommitted-update-invisible contract.
  5. The reader must also NOT block on the writer: the SELECT must
     return promptly (well below SQLite's ``busy_timeout`` /
     30s ``timeout``).  We assert the elapsed time is well under the
     WAL reader-doesn't-block threshold.

This is the negative half of the VP-014 pair.  Its mirror
(``test_committed_update_visible_to_second_connection.py``) proves
the positive half: after COMMIT, the same second-connection SELECT
must observe the new value.

The test command from the verification plan is::

    pytest tests/unit/repositories/test_uncommitted_update_invisible_to_second_connection.py -v
"""

from __future__ import annotations

import time
from pathlib import Path

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate


# The seed value and the dirty value are pinned strings so the
# assertion is byte-for-byte deterministic.  If the schema or the
# routing contract ever changes the seed string, this test must be
# updated to match - the change will be a deliberate, reviewable
# diff in this file rather than a silent loosening of the invariant.
SEED_STAGE = "clean"
DIRTY_STAGE = "dirty"
SEED_SUBSTAGE = "substage_seed"
SEED_VERSION = 1
SEED_PLAN_ID = "p1"


def _bootstrap(db_path: Path) -> None:
    """Create the schema and insert a committed seed row.

    The seed is committed before either the writer or the reader is
    opened, so both connections see a stable baseline.  Closing the
    bootstrap connection matters - leaving it open would keep a
    write lock and serialise the test trivially.
    """
    bootstrap = open_db(db_path)
    try:
        migrate(bootstrap)
        bootstrap.execute(
            "INSERT INTO plan_routing "
            "(plan_id, current_phase, substage, version, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                SEED_PLAN_ID,
                SEED_STAGE,
                SEED_SUBSTAGE,
                SEED_VERSION,
                "2026-08-05T10:00:00Z",
            ),
        )
    finally:
        bootstrap.close()


def test_uncommitted_update_invisible_to_second_connection(tmp_path: Path) -> None:
    """A pending UPDATE on connection A is invisible to connection B.

    The writer opens ``BEGIN IMMEDIATE`` and UPDATEs the row but
    never commits.  The reader, on an independent connection, must
    observe the **old** committed value.

    The reader also must not block: with WAL journal mode, readers
    and writers do not block each other (the writer appends to the
    WAL while readers continue against the last committed snapshot).
    We assert that the SELECT returns in well under the busy-timeout
    window - a few hundred milliseconds is generous.
    """
    db_path = tmp_path / "state.db"
    _bootstrap(db_path)

    writer = open_db(db_path)
    try:
        # BEGIN IMMEDIATE acquires the writer's lock.  We must NOT
        # commit before the reader has had a chance to SELECT, or
        # the test trivially degenerates into the committed-update
        # path.
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE plan_routing SET current_phase = ? WHERE plan_id = ?",
            (DIRTY_STAGE, SEED_PLAN_ID),
        )

        # Sanity check: the writer, inside its own open transaction,
        # DOES see its own dirty write.  This is the standard SQL
        # "read-your-own-writes" rule and rules out the possibility
        # that the UPDATE silently failed.
        writer_view = writer.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (SEED_PLAN_ID,),
        ).fetchone()
        assert writer_view is not None, (
            "seed row disappeared between bootstrap and writer "
            "BEGIN IMMEDIATE - schema/migration mismatch?"
        )
        assert writer_view[0] == DIRTY_STAGE, (
            "writer's own SELECT after UPDATE did not reflect the "
            "dirty value; UPDATE silently no-op'd?"
        )

        reader = open_db(db_path)
        try:
            started = time.monotonic()
            reader_view = reader.execute(
                "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
                (SEED_PLAN_ID,),
            ).fetchone()
            elapsed = time.monotonic() - started
        finally:
            reader.close()

        assert reader_view is not None, (
            "reader SELECT returned no row; seed row missing or "
            "reader pointed at a different database file?"
        )
        assert reader_view[0] == SEED_STAGE, (
            f"second-connection SELECT observed the uncommitted "
            f"UPDATE: got {reader_view[0]!r}, expected the committed "
            f"baseline {SEED_STAGE!r}.  This is the VP-014 regression: "
            f"readers are seeing writer-private state."
        )

        # Reader must NOT block on the writer.  WAL guarantees this:
        # the writer's uncommitted UPDATE goes into the WAL but the
        # reader's snapshot still references the pre-UPDATE page.  A
        # healthy SELECT roundtrip on tmpfs is sub-100ms; we leave
        # generous headroom (1s) so a slow CI host does not flake
        # the test, but anything > 5s would be a regression in the
        # WAL contract.
        assert elapsed < 1.0, (
            f"second-connection SELECT took {elapsed:.3f}s - reader "
            f"appears to be blocked by the writer; WAL reader-doesn't-"
            f"block contract violated."
        )
    finally:
        # Roll back the writer so the test never leaves a dirty
        # transaction behind.  A leftover writer transaction would
        # keep the WAL file pinned and could spill into the next
        # test via the shared tmp_path.
        try:
            writer.execute("ROLLBACK")
        except Exception:
            pass
        writer.close()
