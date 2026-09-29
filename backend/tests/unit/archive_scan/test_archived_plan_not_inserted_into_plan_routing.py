"""VP-020 anchor (1/2): archived plan does NOT enter SQLite.

The state-machine refactor pins SQLite as the single source of
truth for plan-level state, but it pins a hard boundary:
**archived plans do not enter the four ``plan_*`` tables.** The
cutoff is ``2026-08-05`` (``CUTOFF_2026_08_05``); plans dated
``<= cutoff`` are classified ``"archived"`` and their existence
is captured purely by the directory tree + the requirement text
in ``interview.json``.

This test pins the contract that ``RoutingRepository.list_all``
MUST NOT pull an archived plan into the four SQLite tables.  The
contract is checked by:

  1. Building a fresh, empty SQLite database (migrated).
  2. Pointing ``list_all`` at a ``data_dir`` that contains ONE
     archived plan directory (dirname ``20260801-archived``).
  3. Asserting that the four ``plan_*`` tables are still empty
     after the call (a direct SELECT on each of
     ``plan_routing``, ``plan_execution``, ``plan_verification``,
     ``plan_artifacts`` returns ``[]``).
  4. Asserting the archived plan appears in the ``list_all``
     return value with ``archived=True`` and the directory-derived
     fields only (no SQLite semantics).

The "archived row's ``archived`` flag is derived at read-time
ONLY - it is NOT persisted" property is also checked: even after
``list_all`` runs, the row count in each of the four tables is
zero. If a future refactor accidentally writes
``archived=True`` into ``plan_routing`` (treating it as a sixth
column), this test fails loudly.

Contract surface (the four tables):
  * plan_routing      (RoutingRepository)
  * plan_execution    (ExecutionRepository)
  * plan_verification (VerificationRepository)
  * plan_artifacts    (ArtifactRepository)

The fourth assertion is the new one for VP-020: the artifact
manifest for an archived plan must come from
:func:`state_machine.db.artifact_scan.scan_artifacts` (directory
listing), NOT from the ``plan_artifacts`` table.  This is the
"directory scan rather than plan_artifacts table" half of the
spec.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from state_machine.db.artifact_scan import scan_artifacts
from state_machine.db.archive_scan import CUTOFF_2026_08_05
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.routing_repository import RoutingRepository


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


@pytest.fixture
def archived_plan_dir(tmp_path: Path) -> Path:
    """Create an archived plan directory under ``tmp_path``.

    The dirname ``20260801-archived`` is strictly before the
    2026-08-05 cutoff, so :func:`classify_plan` returns
    ``"archived"``.  We seed ``interview.json`` so the
    ``requirement_first_line`` field is non-empty in the
    ``list_all`` return value.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_dir = plans_dir / "20260801-archived"
    plan_dir.mkdir()
    (plan_dir / "interview.json").write_text(
        json.dumps({
            "requirement": "VP-020 archived plan - must not enter SQLite",
        }),
        encoding="utf-8",
    )
    return plans_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FOUR_PLAN_TABLES: tuple[str, ...] = (
    "plan_routing",
    "plan_execution",
    "plan_verification",
    "plan_artifacts",
)


def _count_rows_in(conn: sqlite3.Connection, table: str) -> int:
    """Return the row count for ``table`` after a fresh scan.

    A direct ``SELECT COUNT(*) FROM <table>`` is the contract-level
    check: even if ``list_all`` writes XML-style metadata, a
    separate ``SELECT`` will see it.  We use ``COUNT(*)`` so the
    test fails loudly with the exact row count, not with a
    type-mismatch error.
    """
    cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cur.fetchone()[0])


def _select_all_rows(conn: sqlite3.Connection, table: str) -> list:
    """Return every column / row of ``table`` as a list of rows.

    We do not force ``dict`` here because the connection's
    ``row_factory`` may not be installed on the raw connection
    fixture.  Returning ``sqlite3.Row`` instances preserves the
    contract that "the call left zero rows" (a non-empty list
    fails the test).
    """
    cur = conn.execute(f"SELECT * FROM {table}")
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_all_does_not_insert_archived_plan_into_plan_routing(
    conn: sqlite3.Connection,
    archived_plan_dir: Path,
) -> None:
    """``list_all`` against an archived plan dir leaves plan_routing empty.

    Contract: archived plans do NOT enter SQLite.  Starting with
    a fresh, migrated database, we call
    ``RoutingRepository.list_all(data_dir=archived_plan_dir)``
    and verify:

      1. plan_routing has zero rows.
      2. plan_execution has zero rows.
      3. plan_verification has zero rows.
      4. plan_artifacts has zero rows.
      5. The archived plan IS returned by ``list_all`` with
         ``archived=True`` and the directory-derived fields.
    """
    repo = RoutingRepository(conn)

    # Sanity check: the DB starts empty (no leak from a previous test).
    for table in _FOUR_PLAN_TABLES:
        assert _count_rows_in(conn, table) == 0, (
            f"precondition violated: {table} should be empty on a "
            f"fresh, migrated DB; got {_count_rows_in(conn, table)} rows"
        )

    # Run list_all against the data_dir that contains ONE archived plan.
    result = repo.list_all(data_dir=archived_plan_dir)

    # Direct SELECT on plan_routing: the archived plan_id must NOT exist.
    routing_rows = _select_all_rows(conn, "plan_routing")
    assert routing_rows == [], (
        f"plan_routing must be empty after list_all() against an "
        f"archived-only data_dir; got {routing_rows!r}"
    )

    # Direct SELECT on the other three plan_* tables.
    for table in _FOUR_PLAN_TABLES:
        rows = _select_all_rows(conn, table)
        assert rows == [], (
            f"{table} must remain empty after list_all() against an "
            f"archived-only data_dir; got {rows!r}"
        )

    # The archived plan itself IS returned by list_all - but it is
    # surfaced as a directory-derived row, not as a SQLite row.
    assert len(result) == 1, (
        f"list_all must return exactly one archived row; got "
        f"{len(result)} rows: {result!r}"
    )
    archived = result[0]
    assert archived["plan_id"] == "20260801-archived", (
        f"archived plan_id expected in result; got {archived!r}"
    )
    assert archived["archived"] is True, (
        f"archived row must carry archived=True; got {archived!r}"
    )
    # The archived row must NOT leak SQLite semantics.
    for sqlite_key in ("stage", "substage", "version", "current_phase"):
        assert sqlite_key not in archived, (
            f"archived row must NOT carry SQLite-semantic key "
            f"{sqlite_key!r}; got {archived!r}"
        )

    # The requirement_first_line is derived from the directory, not
    # from any table.
    assert (
        archived["requirement_first_line"]
        == "VP-020 archived plan - must not enter SQLite"
    ), (
        f"requirement_first_line must come from interview.json, got "
        f"{archived.get('requirement_first_line')!r}"
    )


def test_archived_flag_is_read_time_only_not_persisted(
    conn: sqlite3.Connection,
    archived_plan_dir: Path,
) -> None:
    """``archived=True`` lives in the return value, never in the DB.

    The spec explicitly requires:
      ``archived=true only appears in the return value, not written back to DB``

    This test pins that property by running ``list_all`` once and
    then asserting:

      1. None of the four plan_* tables gained an ``archived``
         column (the schema is fixed by :func:`migrate`; a
         regression that adds an ``archived`` column would break
         other tests, but this test pins the contract at the
         SQLite level too).
      2. Every row in the four tables still has zero rows.
    """
    # Confirm the schema does NOT contain an ``archived`` column on
    # any of the four plan_* tables.
    for table in _FOUR_PLAN_TABLES:
        cur = conn.execute(f"PRAGMA table_info({table})")
        cols = [row[1] for row in cur.fetchall()]
        assert "archived" not in cols, (
            f"{table}.archived must NOT be a column; schema is "
            f"fixed by migrate().  Got columns={cols!r}"
        )

    repo = RoutingRepository(conn)
    repo.list_all(data_dir=archived_plan_dir)

    for table in _FOUR_PLAN_TABLES:
        rows = _select_all_rows(conn, table)
        assert rows == [], (
            f"{table} must remain empty after list_all() (no DB "
            f"writes allowed for archived plans); got {rows!r}"
        )


def test_archived_artifact_manifest_uses_directory_scan_not_table(
    conn: sqlite3.Connection,
    archived_plan_dir: Path,
) -> None:
    """Archived plan's artifact manifest comes from directory scan.

    The spec explicitly requires:
      ``archived artifact manifest comes from directory scan, not plan_artifacts table``

    The published artifact-status API for an archived plan is
    :func:`state_machine.db.artifact_scan.scan_artifacts`.  We
    verify both halves of the contract:

      1. ``scan_artifacts`` against the archived plan_dir returns
         one row per canonical artifact whose ``status`` reflects
         file existence (not the ``plan_artifacts`` table).
      2. After ``list_all`` runs, the ``plan_artifacts`` table is
         still empty - the directory scan is the only source of
         artifact status for archived plans.
    """
    archived_plan = archived_plan_dir / "20260801-archived"

    # Tighten the fixture so ``tasks.json`` exists; its status in
    # the directory-scan manifest must be "present".
    (archived_plan / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8",
    )

    # Half 1: scan_artifacts against the directory.
    manifest = scan_artifacts(archived_plan)
    by_type = {row["artifact_type"]: row for row in manifest}
    assert "tasks" in by_type, (
        f"scan_artifacts must include the 'tasks' artifact; "
        f"got types={sorted(by_type.keys())!r}"
    )
    assert by_type["tasks"]["status"] == "present", (
        f"scan_artifacts must mark 'tasks' as present when "
        f"tasks.json exists; got {by_type['tasks']!r}"
    )
    assert "interview" in by_type, (
        f"scan_artifacts must include the 'interview' artifact; "
        f"got types={sorted(by_type.keys())!r}"
    )
    assert by_type["interview"]["status"] == "present", (
        f"scan_artifacts must mark 'interview' as present when "
        f"interview.json exists; got {by_type['interview']!r}"
    )

    # Half 2: plan_artifacts is NOT consulted (and must remain empty).
    repo = RoutingRepository(conn)
    repo.list_all(data_dir=archived_plan_dir)

    rows = _select_all_rows(conn, "plan_artifacts")
    assert rows == [], (
        f"plan_artifacts must remain empty for archived plans; "
        f"the artifact manifest is derived from directory scan only. "
        f"Got rows={rows!r}"
    )


def test_list_all_archived_plan_does_not_appear_in_routing_table(
    conn: sqlite3.Connection,
    archived_plan_dir: Path,
) -> None:
    """Explicit parametrised coverage of the four-table assertion.

    The spec requires a direct SELECT against each of the four
    tables to confirm the archived plan_id is **not present as a
    row** (not even with a `stage = 'archived'` sentinel).  This
    test is the parametrised assertion that reinforces the
    contract - if a future refactor adds a row with
    ``stage='archived'`` to ``plan_routing`` (the alternative
    design that was rejected), this test fails immediately.
    """
    repo = RoutingRepository(conn)
    repo.list_all(data_dir=archived_plan_dir)

    for table in _FOUR_PLAN_TABLES:
        cur = conn.execute(
            f"SELECT plan_id FROM {table} WHERE plan_id = ?",
            ("20260801-archived",),
        )
        found = cur.fetchall()
        assert found == [], (
            f"archived plan_id must NOT appear in {table} at all "
            f"(no 'archived' sentinel row either); got {found!r}"
        )


def test_archived_plan_id_is_not_in_routing_table_after_multiple_calls(
    conn: sqlite3.Connection,
    archived_plan_dir: Path,
) -> None:
    """Re-running ``list_all`` does not regress toward writing rows.

    A common regression mode is "looks fine on the first call,
    starts writing under load".  Two consecutive ``list_all``
    calls must keep the four tables empty - the read-only
    contract on the archive side is enforced on every call.
    """
    repo = RoutingRepository(conn)

    # First call - seeds nothing.
    first = repo.list_all(data_dir=archived_plan_dir)
    assert len(first) == 1
    assert first[0]["archived"] is True

    for table in _FOUR_PLAN_TABLES:
        assert _count_rows_in(conn, table) == 0, (
            f"{table} should be empty after first list_all call; "
            f"got {_count_rows_in(conn, table)} rows"
        )

    # Second call - still seeds nothing.
    second = repo.list_all(data_dir=archived_plan_dir)
    assert len(second) == 1
    assert second[0]["archived"] is True

    for table in _FOUR_PLAN_TABLES:
        assert _count_rows_in(conn, table) == 0, (
            f"{table} must remain empty after second list_all call; "
            f"got {_count_rows_in(conn, table)} rows"
        )


def test_cutoff_constant_is_pinned() -> None:
    """Regression guard: the cutoff is 2026-08-05, not a fresh datetime.

    If a future refactor replaces ``CUTOFF_2026_08_05`` with
    ``datetime.now()`` (the bug 5 regression mode), the cutoff
    would silently advance and every existing archived plan would
    flip to ``"new"`` - silently resurrecting their SQLite-state
    semantics.  Pin the constant.
    """
    from datetime import datetime

    assert CUTOFF_2026_08_05 == datetime(2026, 8, 5), (
        f"CUTOFF_2026_08_05 must remain pinned to 2026-08-05; "
        f"got {CUTOFF_2026_08_05.isoformat()!r}."
    )
