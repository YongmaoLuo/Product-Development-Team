"""VP-015 anchor: ``busy_timeout = 5000`` is applied on a fresh ``state.db``.

This is the L4 storage-contract anchor (VP-015, automated_test, medium
priority).  The verification plan's expected result is:

    "PRAGMA busy_timeout 返回 5000"

The state-machine refactor pins ``busy_timeout = 5000`` ms as one of
the four non-negotiable PRAGMAs that ``state_machine.db.connection.
open()`` applies on every fresh connection.  Without it, short
contention bursts on the SQLite file (planner / executor /
verification threads can all touch the DB at once) would surface as
``OperationalError: database is locked`` to the repository layer.

This test creates an empty ``state.db`` via the public factory and
asserts the live connection reports ``busy_timeout == 5000``.

Why this is its own test (not folded into the connection tests):

  * The verification plan calls out the exact numeric value (5000)
    explicitly, so a regression that silently changes the timeout to
    1000 or 30000 must be caught.
  * SQLite stores busy_timeout internally in **milliseconds**, so the
    assertion is direct -- no unit conversion is needed.

Test command (from the verification plan)::

    pytest tests/unit/db/test_busy_timeout_pragma_applied.py -v
"""

from __future__ import annotations

from pathlib import Path

from state_machine.db.connection import open as open_db

EXPECTED_BUSY_TIMEOUT_MS = 5000


def test_busy_timeout_is_5000_on_fresh_db(tmp_path: Path) -> None:
    """A brand-new ``state.db`` reports ``busy_timeout == 5000``.

    Steps:

      1. Create an empty file at ``tmp_path / 'state.db'``.
      2. Open it via :func:`state_machine.db.connection.open` -- this
         applies the four pinned PRAGMAs, including
         ``PRAGMA busy_timeout = 5000``.
      3. Read ``PRAGMA busy_timeout`` back through the same
         connection.  SQLite returns the active timeout in
         **milliseconds** as a one-row, one-column integer.
      4. Assert it equals ``5000``.

    The connection is closed via the ``sqlite3.Connection`` context
    manager so no stray file handle is left behind on tmp_path.
    """
    db_path = tmp_path / "state.db"

    with open_db(db_path) as conn:
        cursor = conn.execute("PRAGMA busy_timeout")
        row = cursor.fetchone()

    assert row is not None, "PRAGMA busy_timeout returned no row"
    timeout_ms = row[0]
    # SQLite returns the busy_timeout as an int (milliseconds).
    assert int(timeout_ms) == EXPECTED_BUSY_TIMEOUT_MS, (
        f"Expected busy_timeout == {EXPECTED_BUSY_TIMEOUT_MS} ms on a fresh "
        f"state.db, got {timeout_ms!r}. state_machine.db.connection.open() "
        "must apply 'PRAGMA busy_timeout = 5000' so short contention bursts "
        "do not surface as OperationalError: database is locked to the "
        "repository layer."
    )
