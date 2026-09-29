"""VP-006 - acceptance_4 anchor test 2: SQLite schema table set must
equal exactly the four ``plan_*`` tables plus the migration logbook
``schema_version``.

Background
----------
The state-machine refactor pins SQLite as the single source of truth
for plan-level state.  The ``sqlite_master`` table name set for the
state-machine database MUST be EXACTLY::

    {plan_routing, plan_execution, plan_verification, plan_artifacts,
     schema_version}

i.e. four ``plan_*`` "concern" tables plus one ``schema_version``
migration logbook table.  No legacy ``plans``, ``plan_meta``, or
``plan_activity`` tables are allowed; no extra ``plan_*`` tables are
allowed either.

The brief calls this out as:

    sqlite_master table name set  ==
        {plan_routing, plan_execution, plan_verification,
         plan_artifacts} | migration version tables

What the test does
------------------
1. Build a fresh SQLite state-machine DB in ``tmp_path`` using the
   production helper (``state_machine.db.connection.open``) and run
   the canonical migration (``state_machine.db.schema.migrate``).
2. Query ``sqlite_master`` for user tables (excluding ``sqlite_*``
   system tables).
3. Assert the resulting set equals :data:`EXPECTED_SCHEMA_TABLES`
   exactly - no extras (legacy tables must not appear), no missing
   (the four plan_* + schema_version must all be present).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Backend dir on sys.path so we can import state_machine.db.schema.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402


# ---------------------------------------------------------------------------
# Expected schema
# ---------------------------------------------------------------------------
#
# The four ``plan_*`` tables own one concern each:
#   * plan_routing      - current phase / substage / version
#   * plan_execution    - run / pid / attempt counts
#   * plan_verification - verification round / status
#   * plan_artifacts    - artifact status / content_hash
# plus the ``schema_version`` migration logbook, and the v4
# ``plan_tasks`` table (per-task runtime status lifted out of
# tasks.json by the task #v4 split — see
# ``state_machine/db/schema.py`` and ``task_manager``'s
# ``_persist_status_to_sqlite``).
EXPECTED_SCHEMA_TABLES: frozenset[str] = frozenset(
    {
        "plan_routing",
        "plan_execution",
        "plan_verification",
        "plan_artifacts",
        "plan_tasks",
        "schema_version",
    }
)


# ---------------------------------------------------------------------------
# The acceptance gate
# ---------------------------------------------------------------------------

#: Standard pytest markers - drive this test into the correct
#: collection bucket.  ``acceptance_4`` is the L5 anchor that VP-006
#: selects via ``-m acceptance_4``.
pytestmark = [
    pytest.mark.acceptance_4,
]


@pytest.mark.acceptance_4
def test_sqlite_schema_tables_exactly_four_plus_version(tmp_path: Path) -> None:
    """``sqlite_master`` table name set must equal exactly the four
    ``plan_*`` tables plus the migration logbook ``schema_version``.

    Implementation:

      1. Open a fresh SQLite database at ``tmp_path / state.db``
         using the production helper :func:`open_db`.
      2. Run :func:`migrate` to create the canonical four tables and
         the ``schema_version`` migration row.
      3. Query ``sqlite_master`` for user tables (excluding
         ``sqlite_*`` system tables).
      4. Assert the set equals :data:`EXPECTED_SCHEMA_TABLES`
         exactly - no extras (no legacy ``plans`` / ``plan_meta`` /
         ``plan_activity``), no missing.
    """
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        names = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

    assert names == EXPECTED_SCHEMA_TABLES, (
        f"schema table set must equal exactly "
        f"{set(EXPECTED_SCHEMA_TABLES)!r}; got {sorted(names)!r}.  "
        f"Legacy tables (plans, plan_meta, plan_activity) must not "
        f"appear, and the four plan_* + schema_version tables must "
        f"all be present."
    )


@pytest.mark.acceptance_4
def test_legacy_tables_absent_from_schema(tmp_path: Path) -> None:
    """The state-machine schema must not contain legacy ``plans``,
    ``plan_meta``, or ``plan_activity`` tables.

    This is the negative side of the schema contract - even if the
    refactor adds new ``plan_*`` tables, the legacy names must stay
    absent.
    """
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        names = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

    for legacy in ("plans", "plan_meta", "plan_activity"):
        assert legacy not in names, (
            f"legacy table {legacy!r} appears in sqlite_master; "
            f"the state-machine refactor forbids it.  Found tables: "
            f"{sorted(names)!r}"
        )
