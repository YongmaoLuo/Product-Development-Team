"""VP-015 anchor: WAL journal mode is enabled on a fresh ``state.db``.

This is the L4 storage-contract anchor (VP-015, automated_test, medium
priority).  The verification plan's expected result is:

    "新建 state.db 后 PRAGMA journal_mode 返回 wal"

The state-machine refactor pins WAL as one of the four non-negotiable
PRAGMAs that ``state_machine.db.connection.open()`` applies on every
fresh connection.  This test creates an empty ``state.db`` via the
public factory and asserts the live connection reports
``journal_mode == 'wal'``.

Why this is its own test (not folded into the connection tests):

  * The verification plan calls out the *fresh* DB case explicitly --
    a regression where the PRAGMA was silently dropped because the
    connection was opened against an already-WALed file (in which case
    SQLite returns ``'wal'`` even without re-applying the PRAGMA) must
    still be caught.  Asserting on a fresh DB forces the test to
    exercise the code path that applies the PRAGMA.
  * It is a single, laser-focused invariant -- easy to grep for, easy
    to triage if it fails, and easy to keep stable across refactors.

Test command (from the verification plan)::

    pytest tests/unit/db/test_wal_mode_enabled_on_fresh_db.py -v
"""

from __future__ import annotations

from pathlib import Path

from state_machine.db.connection import open as open_db


def test_journal_mode_is_wal_on_fresh_db(tmp_path: Path) -> None:
    """A brand-new ``state.db`` reports ``journal_mode == 'wal'``.

    Steps:

      1. Create an empty file at ``tmp_path / 'state.db'`` (parent dir
         is auto-created by :func:`open`).
      2. Open it via :func:`state_machine.db.connection.open` -- this
         applies the four pinned PRAGMAs from the contract.
      3. Read ``PRAGMA journal_mode`` back through the same connection.
         SQLite returns the *active* journal mode as a one-row, one-
         column result, lower-case.
      4. Assert it equals ``'wal'``.

    The connection is closed via the ``sqlite3.Connection`` context
    manager so no stray file handle is left behind on tmp_path.
    """
    db_path = tmp_path / "state.db"

    with open_db(db_path) as conn:
        cursor = conn.execute("PRAGMA journal_mode")
        row = cursor.fetchone()

    assert row is not None, "PRAGMA journal_mode returned no row"
    mode = row[0]
    assert isinstance(mode, str), (
        f"PRAGMA journal_mode should return a string, got {type(mode).__name__}"
    )
    assert mode.lower() == "wal", (
        f"Expected journal_mode == 'wal' on a fresh state.db, got {mode!r}. "
        "state_machine.db.connection.open() must apply 'PRAGMA journal_mode = WAL' "
        "so concurrent readers (planner / executor / verification threads) do "
        "not block writers."
    )
