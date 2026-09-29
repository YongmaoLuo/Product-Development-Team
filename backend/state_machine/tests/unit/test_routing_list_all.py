"""Unit tests for :meth:`RoutingRepository.list_all` archive-merge contract.

Background
----------
Task 2 originally implemented ``RoutingRepository.list_all`` as a
placeholder: it returned every row from ``plan_routing`` with
``archived = False`` derived at the cutoff.  Task 6 wired the
``scan_archived_plans`` boundary-value judgement (8 月 5 日 cutoff).
Task 7 (this file's subject) closes the loop:

  * The repository's ``list_all`` must read SQLite AND the directory
    scan, then merge them at the boundary:

      - new plans: full routing columns (``plan_id``, ``stage``,
        ``substage``, ``version``, ``updated_at``, ``archived=False``)
      - archived plans: only directory metadata (``plan_id``,
        ``requirement_first_line``, ``created_at``, ``archived=True``);
        **never** ``stage`` / ``current_phase`` fields (archived plans
        do not enter SQLite, so the routing columns are undefined)

  * The repository must NOT write the ``archived`` flag back to
    SQLite.  The flag is derived at read-time only.

  * If a plan is in BOTH the SQLite table and the directory scan,
    the archive version wins (the SQLite row is a stale bootstrap).

  * Sorting: SQLite rows by ``updated_at`` DESC, archived rows by
    ``created_at`` DESC.  Two sort keys, two homogeneous lists.

  * ``data_dir is None`` -> archive scan is skipped (SQLite-only).
  * ``data_dir`` does not exist -> archive scan returns ``[]``;
    SQLite-only path.

TDD spec (the tests this file pins):

  * test_list_all_includes_archived_with_flag
      Calling list_all(data_dir=...) on a SQLite row + an archived
      directory returns BOTH; the archived row carries archived=True.

  * test_list_all_archived_entry_has_no_stage_field
      (negative assertion)  archived entry dict has NO 'stage' /
      'current_phase' key.

  * test_list_all_does_not_write_archived_flag_to_db
      Calling list_all does not write 'archived' to plan_routing
      (column does not exist; SELECT archived FROM plan_routing
      returns 0 rows).

  * test_list_all_orders_new_first_then_archived_by_created_at_desc
      Combined list is sorted: SQLite (updated_at desc) FIRST, then
      archived (created_at desc).  Or alternatively, an interleaved
      sort where each section respects its own key.  The pinned
      contract is: the archived list (sorted by created_at desc)
      comes AFTER the SQLite list (sorted by updated_at desc); a
      SQLite plan dated ``T`` is always ahead of an archived plan
      dated ``T' > T``?  Actually the spec says "new first, then
      archived by created_at desc" which is what we test.

  * test_list_all_without_data_dir_returns_sqlite_only
      data_dir=None -> only SQLite rows; no archive-scan call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.routing_repository import (
    RoutingRepository,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a fresh, migrated SQLite connection (autocommit mode)."""
    db_path = tmp_path / "state.db"
    connection = open_db(db_path)
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


def _make_archived_plan(
    data_dir: Path,
    plan_id: str,
    requirement_text: str,
) -> Path:
    """Create an archived plan directory under ``data_dir``.

    The ``plan_id`` is expected to encode the date as ``YYYYMMDD-*``
    so :func:`scan_archived_plans` classifies it as ``archived``
    against the 2026-08-05 cutoff.

    Writes a minimal ``interview.json`` so
    ``requirement_first_line`` is populated.
    """
    plan_dir = data_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    interview_payload = (
        '{"requirement": "' + requirement_text + '"}'
    )
    (plan_dir / "interview.json").write_text(
        interview_payload, encoding="utf-8"
    )
    return plan_dir


# ---------------------------------------------------------------------------
# 1) Archived row appears with archived=True
# ---------------------------------------------------------------------------


def test_list_all_includes_archived_with_flag(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """``list_all(data_dir=...)`` includes the archived row with archived=True.

    Setup:
      * SQLite has one new plan ("new-plan", stage=ready)
      * data_dir has one archived plan ("20260801-archived-plan")
        whose dirname is BEFORE the 2026-08-05 cutoff.

    Expectation:
      * list_all returns 2 rows
      * the archived row carries archived=True
      * the new row carries archived=False
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="new-plan", phase="ready", substage=None)

    data_dir = tmp_path / "plans"
    _make_archived_plan(
        data_dir,
        "20260801-archived-plan",
        "archived requirement first line",
    )

    rows = repo.list_all(data_dir=data_dir)
    by_id = {r["plan_id"]: r for r in rows}
    assert set(by_id.keys()) == {"new-plan", "20260801-archived-plan"}
    assert by_id["new-plan"]["archived"] is False
    assert by_id["20260801-archived-plan"]["archived"] is True


# ---------------------------------------------------------------------------
# 2) Archived row has NO 'stage' / 'current_phase' field key
# ---------------------------------------------------------------------------


def test_list_all_archived_entry_has_no_stage_field(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Archived row dict MUST NOT carry a 'stage' or 'current_phase' key.

    Archived plans do NOT enter the SQLite state machine — they have
    no ``stage`` column to read.  Returning ``stage`` on the archived
    entry would leak SQLite semantics and let callers mistakenly try
    to ``try_mark_phase`` on a 410 Gone plan.

    Negative assertion: ``assert "stage" not in archived`` and
    ``assert "current_phase" not in archived``.
    """
    repo = RoutingRepository(conn)

    data_dir = tmp_path / "plans"
    _make_archived_plan(
        data_dir,
        "20260801-archived-plan",
        "first line of requirement",
    )

    rows = repo.list_all(data_dir=data_dir)
    assert len(rows) == 1, f"expected exactly 1 archived row; got {len(rows)}"
    archived = rows[0]

    assert "stage" not in archived, (
        f"archived row must NOT carry a 'stage' key; got {archived!r}"
    )
    assert "current_phase" not in archived, (
        f"archived row must NOT carry a 'current_phase' key; got {archived!r}"
    )

    # Sanity: the required directory-derived keys ARE present.
    assert "plan_id" in archived
    assert "archived" in archived
    assert "requirement_first_line" in archived
    assert "created_at" in archived


# ---------------------------------------------------------------------------
# 3) list_all must NOT write 'archived' into plan_routing
# ---------------------------------------------------------------------------


def test_list_all_does_not_write_archived_flag_to_db(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Calling ``list_all`` does NOT add an ``archived`` column/row to SQLite.

    We pin the negative contract: the ``archived`` flag is derived at
    read-time, NOT persisted.  This is the bug that would otherwise
    silently couple the routing table to the directory tree.

    Implementation: run ``SELECT archived FROM plan_routing WHERE
    plan_id = ?``.  The ``archived`` column does not exist on
    ``plan_routing`` (the schema only has plan_id, stage, substage,
    version, updated_at), so this query returns 0 rows OR raises
    OperationalError depending on the SQLite compile setting.  We
    accept either outcome — the strict pass condition is that the
    query does NOT return a row with archived=True.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="new-plan", phase="ready", substage=None)

    data_dir = tmp_path / "plans"
    _make_archived_plan(
        data_dir,
        "20260801-archived-plan",
        "archived requirement",
    )

    # Trigger the merge.
    rows = repo.list_all(data_dir=data_dir)
    assert len(rows) == 2

    # The archived plan_id must NOT have a row in plan_routing
    # after the merge.
    cur = conn.execute(
        "SELECT plan_id FROM plan_routing WHERE plan_id = ?",
        ("20260801-archived-plan",),
    )
    assert cur.fetchone() is None, (
        "plan_routing must not have a row for the archived plan_id; "
        "list_all must NOT write archived plans to SQLite"
    )

    # And selecting the non-existent 'archived' column either fails
    # (OperationalError: no such column) or returns 0 rows — either
    # is fine.  The strict check: at most 0 rows with archived=True
    # come back from plan_routing.
    try:
        cur = conn.execute(
            "SELECT archived FROM plan_routing WHERE plan_id = ?",
            ("20260801-archived-plan",),
        )
        rows_after = cur.fetchall()
        assert len(rows_after) == 0, (
            f"plan_routing must not store archived=True; got {rows_after!r}"
        )
    except sqlite3.OperationalError:
        # ``archived`` column does not exist — that's the desired
        # state.  The contract is honoured either way.
        pass


# ---------------------------------------------------------------------------
# 4) Sort order: SQLite (updated_at desc) first, archived (created_at desc)
# ---------------------------------------------------------------------------


def test_list_all_orders_new_first_then_archived_by_created_at_desc(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The combined list is ordered: SQLite rows by ``updated_at`` desc,
    then archived rows by ``created_at`` desc.

    Setup:
      * SQLite plan "new-plan" (will have updated_at = now)
      * SQLite plan "older-plan" (updated_at older)
      * archived plan "20260803-a-archived" (created_at = 2026-08-03)
      * archived plan "20260801-b-archived" (created_at = 2026-08-01)

    Expectation:
      * The two SQLite rows come first, ordered by updated_at desc
        (new-plan before older-plan).
      * The two archived rows follow, ordered by created_at desc
        (20260803-a-archived before 20260801-b-archived).
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="older-plan", phase="executing", substage=None)
    # Insert the new-plan a moment later so its updated_at is
    # strictly greater than older-plan's.
    repo.insert(plan_id="new-plan", phase="ready", substage=None)

    data_dir = tmp_path / "plans"
    _make_archived_plan(
        data_dir,
        "20260803-a-archived",
        "req a",
    )
    _make_archived_plan(
        data_dir,
        "20260801-b-archived",
        "req b",
    )

    rows = repo.list_all(data_dir=data_dir)
    plan_ids_in_order = [r["plan_id"] for r in rows]

    # SQLite section: newer-plan must come before older-plan.
    sqlite_section = [pid for pid in plan_ids_in_order
                      if pid in {"new-plan", "older-plan"}]
    assert sqlite_section == ["new-plan", "older-plan"], (
        f"SQLite rows must be ordered by updated_at desc; "
        f"got {sqlite_section!r}"
    )

    # Archived section: later created_at must come before earlier.
    archived_section = [pid for pid in plan_ids_in_order
                        if pid in {"20260803-a-archived",
                                   "20260801-b-archived"}]
    assert archived_section == ["20260803-a-archived",
                                "20260801-b-archived"], (
        f"archived rows must be ordered by created_at desc; "
        f"got {archived_section!r}"
    )

    # And the SQLite section must precede the archived section.
    last_sqlite_idx = max(
        plan_ids_in_order.index(pid)
        for pid in ("new-plan", "older-plan")
    )
    first_archived_idx = min(
        plan_ids_in_order.index(pid)
        for pid in ("20260803-a-archived", "20260801-b-archived")
    )
    assert last_sqlite_idx < first_archived_idx, (
        f"SQLite rows must precede archived rows; got "
        f"plan_ids_in_order={plan_ids_in_order!r}"
    )


# ---------------------------------------------------------------------------
# 5) data_dir=None -> SQLite-only (no archive scan call)
# ---------------------------------------------------------------------------


def test_list_all_without_data_dir_returns_sqlite_only(
    conn: sqlite3.Connection,
) -> None:
    """Calling ``list_all(data_dir=None)`` returns ONLY the SQLite rows.

    The archive-scan side must be skipped entirely.  This is the
    safe default for callers that don't have a directory tree to
    scan (e.g. legacy tests, callers that only care about active
    routing).

    We verify by:
      1. Inserting two SQLite plans.
      2. Calling list_all(data_dir=None).
      3. Confirming exactly 2 rows, all with archived=False.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="alpha", phase="ready", substage=None)
    repo.insert(plan_id="bravo", phase="executing", substage=None)

    rows = repo.list_all(data_dir=None)

    assert len(rows) == 2
    by_id = {r["plan_id"]: r for r in rows}
    assert by_id["alpha"]["archived"] is False
    assert by_id["bravo"]["archived"] is False
    # 2026-09-17 (schema v5): the row exposes the single
    # workflow-state column, not the retired ``stage`` alias.
    assert "current_phase" in by_id["alpha"]
    assert "current_phase" in by_id["bravo"]


# ---------------------------------------------------------------------------
# Extra coverage: data_dir path does not exist
# ---------------------------------------------------------------------------


def test_list_all_with_missing_data_dir_returns_sqlite_only(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A non-existent ``data_dir`` is treated as ``[]`` — SQLite-only path.

    The contract: ``scan_archived_plans`` returns ``[]`` when
    ``data_dir`` does not exist, so the merge just degenerates to
    the SQLite-only output.  This is the safe side: a missing
    directory tree must not break the read API.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="only-plan", phase="ready", substage=None)

    nonexistent_dir = tmp_path / "this_path_does_not_exist"
    assert not nonexistent_dir.exists()

    rows = repo.list_all(data_dir=nonexistent_dir)

    assert len(rows) == 1
    assert rows[0]["plan_id"] == "only-plan"
    assert rows[0]["archived"] is False


# ---------------------------------------------------------------------------
# Extra coverage: archived plan_id also in SQLite -> archive wins
# ---------------------------------------------------------------------------


def test_list_all_archived_in_sqlite_uses_archived_version(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """If a plan_id appears in BOTH SQLite and the directory scan,
    the archived version wins (no duplicate row, no SQLite row).

    This is the "stale bootstrap" edge case: the plan was archived
    (directory renamed with old date prefix) but the SQLite row
    was never cleaned up.  The merge layer must treat the directory
    tree as authoritative for archived entries and emit exactly
    ONE row per archived plan_id — the directory metadata one.

    The SQLite row is left untouched (no DELETE) — the bootstrap
    cleanup is a separate concern; this merge just refuses to
    surface it twice.
    """
    repo = RoutingRepository(conn)
    # The same plan_id appears in SQLite.
    repo.insert(plan_id="20260801-stale-bootstrap", phase="ready",
                substage=None)

    data_dir = tmp_path / "plans"
    _make_archived_plan(
        data_dir,
        "20260801-stale-bootstrap",
        "bootstrap requirement",
    )

    rows = repo.list_all(data_dir=data_dir)

    # Exactly one row in the merged output (the archived version).
    by_id = {r["plan_id"]: r for r in rows}
    assert set(by_id.keys()) == {"20260801-stale-bootstrap"}
    archived = by_id["20260801-stale-bootstrap"]
    assert archived["archived"] is True
    assert "stage" not in archived, (
        "stale-bootstrap merged row must be the archived version, "
        "not the SQLite version"
    )
    assert "requirement_first_line" in archived