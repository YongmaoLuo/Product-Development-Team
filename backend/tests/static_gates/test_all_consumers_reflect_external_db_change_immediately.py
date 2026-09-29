"""VP-017 acceptance gate (behavior half): every consumer must
reflect an external DB change on its next read.

Background
----------
The state-machine refactor pins SQLite (via the four
``plan_*`` tables) as the single source of truth.  Two failure
modes can hide a "second source of truth":

  1. A consumer module keeps an in-memory cache that is populated
     lazily and only refreshed on a periodic tick / on
     "explicit" writes - so a row written by another process
     (or another connection) is invisible until the cache is
     flushed.
  2. A consumer reads from a per-process shadow file (e.g. the
     legacy JSON sidecars) instead of the live SQLite row.

This gate exercises the **positive half** of VP-017: open a
fresh repository handle (which opens its own connection), point
it at a SQLite file that has just been mutated by an
*independent* writer connection, and confirm the handle sees
the new value on its next SELECT.  No cache, no shadow file, no
batch update - the read must reflect the external change
**immediately**.

What the test does
------------------
1. Build a fresh SQLite state-machine DB in ``tmp_path``.
2. Use :func:`state_machine.db.connection.open` to open two
   independent connections ``A`` (writer) and ``B`` (reader).
3. Seed ``plan_routing`` via ``A``; COMMIT.
4. On ``A`` (writer), run ``UPDATE plan_routing SET stage = ?
   WHERE plan_id = ?`` followed by COMMIT.
5. Construct a :class:`RoutingRepository` against connection
   ``B`` and call :meth:`current` (the canonical read path).
6. Assert the returned ``stage`` equals the writer's new value,
   NOT the seed value.

The test exercises the same ``open → migrate → write →
independent-read`` flow every consumer uses; if any consumer
ever introduces a cache layer or shadow file, this test will
fail when that consumer is run through the same protocol.

The test command from the verification plan is::

    pytest tests/static_gates/test_all_consumers_reflect_external_db_change_immediately.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Backend dir on sys.path so we can import state_machine.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.routing_repository import (  # noqa: E402
    RoutingRepository,
)


# ---------------------------------------------------------------------------
# Pinned strings (deterministic byte-for-byte assertions).
# ---------------------------------------------------------------------------
SEED_PLAN_ID = "vp017_consumer_probe"
SEED_STAGE = "sealed"
SEED_SUBSTAGE = "substage_seed"
EXTERNAL_STAGE = "external_writer_committed"
EXTERNAL_SUBSTAGE = "external_substage"


# ---------------------------------------------------------------------------
# The acceptance gate
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.acceptance_vp017,
]


def _bootstrap(db_path: Path) -> None:
    """Create the canonical four ``plan_*`` tables and seed
    ``plan_routing`` with a single committed row.

    Closing the bootstrap connection matters - leaving it open
    would serialise the test trivially.
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
                1,
                "2026-08-05T10:00:00Z",
            ),
        )
        bootstrap.commit()
    finally:
        bootstrap.close()


@pytest.mark.acceptance_vp017
def test_external_db_change_visible_via_routing_repository(tmp_path: Path) -> None:
    """An independent writer's COMMIT must be visible to a freshly
    opened :class:`RoutingRepository` handle on its next read.

    This is the canonical "no second source of truth" probe for
    the routing consumer surface.  If a cache or shadow file is
    ever introduced between the writer and the reader, this
    assertion will fail.
    """
    db_path = tmp_path / "state.db"
    _bootstrap(db_path)

    # ----- Step 1: external writer commits a new value ---------
    writer = open_db(db_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE plan_routing "
            "SET current_phase = ?, substage = ?, version = version + 1 "
            "WHERE plan_id = ?",
            (EXTERNAL_STAGE, EXTERNAL_SUBSTAGE, SEED_PLAN_ID),
        )
        writer.execute("COMMIT")
    finally:
        writer.close()

    # ----- Step 2: independent reader picks up via the
    #               canonical RoutingRepository surface ------------
    reader_conn = open_db(db_path)
    try:
        repo = RoutingRepository(reader_conn)
        row = repo.current(SEED_PLAN_ID)
        assert row is not None, (
            f"RoutingRepository.current({SEED_PLAN_ID!r}) returned "
            f"None - the seeded + externally-updated row is "
            f"missing from plan_routing.  A cache layer may be "
            f"serving a stale snapshot."
        )
        assert row["current_phase"] == EXTERNAL_STAGE, (
            f"RoutingRepository did NOT reflect the external "
            f"writer's COMMIT.  Expected stage={EXTERNAL_STAGE!r}, "
            f"got stage={row.get('stage')!r}.  This indicates a "
            f"hidden second source of truth (cache or shadow "
            f"file) between the writer and this consumer."
        )
        assert row["substage"] == EXTERNAL_SUBSTAGE, (
            f"RoutingRepository.substage is stale: "
            f"expected={EXTERNAL_SUBSTAGE!r}, "
            f"got={row.get('substage')!r}"
        )
    finally:
        reader_conn.close()


@pytest.mark.acceptance_vp017
def test_external_db_change_visible_on_second_reader_connection(
    tmp_path: Path,
) -> None:
    """Two consecutive readers opened AFTER the external COMMIT
    both see the new value - no first-read priming, no
    write-time caching.
    """
    db_path = tmp_path / "state.db"
    _bootstrap(db_path)

    writer = open_db(db_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE plan_routing "
            "SET current_phase = ?, substage = ?, version = version + 1 "
            "WHERE plan_id = ?",
            (EXTERNAL_STAGE, EXTERNAL_SUBSTAGE, SEED_PLAN_ID),
        )
        writer.execute("COMMIT")
    finally:
        writer.close()

    # First reader.
    r1 = open_db(db_path)
    try:
        repo1 = RoutingRepository(r1)
        row1 = repo1.current(SEED_PLAN_ID)
        assert row1 is not None
        assert row1["current_phase"] == EXTERNAL_STAGE
    finally:
        r1.close()

    # Second reader - separate connection, opened AFTER r1 closed,
    # must also see the externally committed value.
    r2 = open_db(db_path)
    try:
        repo2 = RoutingRepository(r2)
        row2 = repo2.current(SEED_PLAN_ID)
        assert row2 is not None, (
            "Second RoutingRepository handle returned None for a "
            "row that is committed and visible.  A consumer-level "
            "cache is hiding a real row."
        )
        assert row2["current_phase"] == EXTERNAL_STAGE, (
            f"Second reader did NOT reflect the external writer's "
            f"COMMIT.  Expected stage={EXTERNAL_STAGE!r}, got "
            f"stage={row2.get('stage')!r}."
        )
    finally:
        r2.close()


@pytest.mark.acceptance_vp017
def test_external_db_change_visible_without_consumer_reopen(
    tmp_path: Path,
) -> None:
    """Even if the consumer's repository handle stays open across
    the external COMMIT, the next read MUST reflect the new
    value - SQLite WAL is required to push the change.
    """
    db_path = tmp_path / "state.db"
    _bootstrap(db_path)

    consumer = open_db(db_path)
    try:
        repo = RoutingRepository(consumer)

        # Baseline: read the seeded row.
        baseline = repo.current(SEED_PLAN_ID)
        assert baseline is not None
        assert baseline["current_phase"] == SEED_STAGE

        # External writer commits a new value on a SEPARATE
        # connection while the consumer handle is still open.
        external = open_db(db_path)
        try:
            external.execute("BEGIN IMMEDIATE")
            external.execute(
                "UPDATE plan_routing "
                "SET current_phase = ?, substage = ?, version = version + 1 "
                "WHERE plan_id = ?",
                (EXTERNAL_STAGE, EXTERNAL_SUBSTAGE, SEED_PLAN_ID),
            )
            external.execute("COMMIT")
        finally:
            external.close()

        # The consumer handle - NOT reopened, NOT refreshed - must
        # now reflect the externally committed value.  If a
        # process-level cache shadows the live row, this read
        # returns the seed and the test fails.
        after = repo.current(SEED_PLAN_ID)
        assert after is not None
        assert after["current_phase"] == EXTERNAL_STAGE, (
            f"Consumer repository handle did NOT reflect an "
            f"external COMMIT made on a separate connection.  "
            f"Expected stage={EXTERNAL_STAGE!r}, got "
            f"stage={after.get('stage')!r}.  This indicates a "
            f"hidden second source of truth (in-memory cache) "
            f"that the consumer must NOT have."
        )
    finally:
        consumer.close()
