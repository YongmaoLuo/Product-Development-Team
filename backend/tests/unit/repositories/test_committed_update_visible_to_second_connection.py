"""Committed update must be visible to a second connection (VP-014).

Bug-class anchor (VP-014): once a writer connection executes
``COMMIT`` on a transaction that mutated a row, every other
connection in the same database MUST observe the new value on its
next SELECT.  This is the reader side of the durability contract:
a committed change is durable AND visible.

This test pins down the positive half of the VP-014 pair:

  1. Open ``A`` and ``B`` against the same DB file (the WAL contract
     is established by :func:`state_machine.db.connection.open`).
  2. Seed a row in ``plan_routing`` (committed) on a bootstrap
     connection.
  3. On connection ``A``:
       * ``BEGIN IMMEDIATE``
       * ``UPDATE plan_routing SET stage = 'committed_dirty'``
       * ``COMMIT``
  4. On connection ``B`` (an independent connection, opened AFTER
     the COMMIT), a fresh SELECT MUST return the new value
     (``'committed_dirty'``) - not the pre-update value.

The test command from the verification plan is::

    pytest tests/unit/repositories/test_committed_update_visible_to_second_connection.py -v
"""

from __future__ import annotations

from pathlib import Path

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate


# The seed value and the committed-update value are pinned strings
# so the assertion is byte-for-byte deterministic.  If the schema or
# the routing contract ever changes, this test must be updated to
# match - the change will be a deliberate, reviewable diff in this
# file rather than a silent loosening of the invariant.
SEED_STAGE = "clean"
COMMITTED_STAGE = "committed_dirty"
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


def test_committed_update_visible_to_second_connection(tmp_path: Path) -> None:
    """A committed UPDATE on connection A is visible to connection B.

    The writer opens ``BEGIN IMMEDIATE``, UPDATEs the row, then
    COMMITs.  The reader, on a freshly-opened independent connection,
    must observe the new committed value.
    """
    db_path = tmp_path / "state.db"
    _bootstrap(db_path)

    writer = open_db(db_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE plan_routing SET current_phase = ? WHERE plan_id = ?",
            (COMMITTED_STAGE, SEED_PLAN_ID),
        )
        writer.execute("COMMIT")

        # Sanity check: writer, after COMMIT, reads back the new
        # value.  This is trivially true for the writer, but having
        # the assertion in place makes a typo in the UPDATE surface
        # immediately rather than as a confusing "reader saw old
        # value" failure.
        writer_view = writer.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (SEED_PLAN_ID,),
        ).fetchone()
        assert writer_view is not None
        assert writer_view[0] == COMMITTED_STAGE, (
            "writer's post-COMMIT SELECT did not return the new "
            "value; UPDATE or COMMIT silently failed."
        )
    finally:
        writer.close()

    # The reader is opened AFTER the writer has been closed.  A
    # brand-new connection in WAL mode starts against the latest
    # committed snapshot - so it MUST see the committed value.  The
    # test deliberately opens a new connection (rather than reusing
    # the writer) because the VP-014 contract is "independent
    # second connection", and reusing the writer handle would mask
    # any read-snapshot staleness bugs.
    reader = open_db(db_path)
    try:
        reader_view = reader.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (SEED_PLAN_ID,),
        ).fetchone()
    finally:
        reader.close()

    assert reader_view is not None, (
        "reader SELECT returned no row; seed row missing or reader "
        "pointed at a different database file?"
    )
    assert reader_view[0] == COMMITTED_STAGE, (
        f"second-connection SELECT did not observe the COMMITTED "
        f"update: got {reader_view[0]!r}, expected "
        f"{COMMITTED_STAGE!r}.  This is the VP-014 regression: "
        f"committed writes are not being propagated to other "
        f"connections."
    )
    assert reader_view[0] != SEED_STAGE, (
        "second-connection SELECT still saw the pre-update value "
        "after the writer COMMITted.  Either the COMMIT did not "
        "actually flush, or the reader is pinned to a stale "
        "snapshot."
    )
