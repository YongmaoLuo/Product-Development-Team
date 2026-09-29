"""Pointer-not-body anchor for plan_artifacts (VP-021, half A).

A 10MB file body on disk does NOT bloat the ``plan_artifacts``
SQLite table: the table stores the path as a pointer, the body
stays on disk. This is the **pointer-not-body anchor** for VP-021.

The verification command from the verification plan is::

    pytest tests/unit/repositories/test_artifact_row_stores_pointer_not_body.py -v
"""

from __future__ import annotations

from pathlib import Path

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.artifact_repository import ArtifactRepository


def _db_total_size(db_path: Path) -> int:
    """Return the total on-disk footprint of the SQLite database.

    WAL mode writes to a separate ``-wal`` sidecar file, so the main
    DB file's st_size alone understates the actual write volume. We
    sum the main file plus the ``-wal`` and ``-shm`` sidecars so the
    measurement reflects the full footprint the writer is producing.
    """
    total = 0
    for path in (db_path, db_path.with_suffix(db_path.suffix + "-wal"),
                 db_path.with_suffix(db_path.suffix + "-shm")):
        if path.exists():
            total += path.stat().st_size
    return total


def test_artifact_row_stores_pointer_not_body(tmp_path: Path) -> None:
    """A 10MB file body on disk does NOT bloat the database.

    This is the **pointer-not-body anchor**: the table stores the
    path pointing to the file *on disk* - the body itself is NOT
    mirrored into the database. We write a 10MB file under
    ``tmp_path``, then upsert a pointer to it, and assert the DB
    footprint grows by less than 1KB.
    """
    db_path = tmp_path / "state.db"
    body_path = tmp_path / "interview.json"
    body_path.write_bytes(b"x" * (10 * 1024 * 1024))  # 10MB body
    assert body_path.stat().st_size == 10 * 1024 * 1024

    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = ArtifactRepository(conn)
        before = _db_total_size(db_path)

        # Upsert the *pointer* (the path on disk) only.
        repo.upsert(
            plan_id="p1",
            artifact_type="interview",
            file_path=str(body_path),
            status="generated",
            content_hash="abc",
        )

        # Issue a checkpoint so the WAL contents are flushed.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        after = _db_total_size(db_path)
        delta = after - before
        # Anchor: < 1KB of growth proves the body is NOT in the DB.
        assert delta < 1024, (
            f"upsert of a 10MB file pointer grew the DB footprint by "
            f"{delta} bytes; the body must not be stored in the table"
        )

        # Belt-and-suspenders: the row in the table must NOT
        # contain any of the body bytes.
        cur = conn.execute(
            "SELECT file_path FROM plan_artifacts WHERE plan_id = ?",
            ("p1",),
        )
        stored_path = cur.fetchone()[0]
        assert stored_path == str(body_path)
        assert "x" * 1000 not in stored_path, (
            "stored file_path appears to contain body bytes; "
            "the body must not be mirrored into the table"
        )
    finally:
        conn.close()
