"""
TDD tests for ``state_machine.repositories.artifact_repository``.

Background
----------
The state-machine refactor stores the **paths** of every artifact
(interview, prd, arch-design, test-design, tasks, execution.json, etc.)
in the ``plan_artifacts`` table; the artifact bodies live on disk and
are NOT persisted into the database. The :class:`ArtifactRepository`
is the single write path for that table and owns the following
contracts:

  1. ``upsert(plan_id, artifact_type, file_path, status, content_hash)``
     inserts a new row, or — if ``(plan_id, artifact_type)`` already
     exists — updates the existing row in place. The SQL form is
     ``INSERT ... ON CONFLICT(plan_id, artifact_type) DO UPDATE`` so
     the row never duplicates.

  2. The ``status`` column is restricted to the enum
     ``{pending, generated, stale, missing}``. Any other value raises
     :class:`ValueError` *before* the SQL is executed (so a typo in
     the caller never produces a corrupt row).

  3. Only pointers (``file_path`` + ``content_hash``) are stored.
     Inserting a 10MB ``file_path`` string must not grow the database
     by anything close to 10MB — the row size is bounded by the path
     length, NOT the file contents. The body lives on disk.

  4. Concurrent ``upsert`` calls on the same
     ``(plan_id, artifact_type)`` converge to exactly one row.

  5. ``mark_stale`` and ``mark_missing`` flip the ``status`` column
     to the canonical enum value while preserving ``file_path`` and
     ``content_hash``.

  6. ``list_for_plan(plan_id)`` returns all artifacts for one plan
     (or ``[]`` if the plan has none). ``list_for_plans(plan_ids)``
     returns a ``dict[str, list[dict]]`` keyed by plan_id; missing
     plans appear as empty lists.

Edge cases (boundary conditions):
  - Unknown ``status`` -> ``ValueError``
  - Unknown ``plan_id`` in ``list_for_plan`` -> ``[]``
  - 10MB ``file_path`` stored as a pointer -> DB grows < 1KB
  - 8 threads racing on the same key -> 1 row wins
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Local helpers (small / focused — we deliberately do NOT import the
# public test helpers from the parent test module to keep this file
# self-contained and runnable in isolation).
# ---------------------------------------------------------------------------


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    """Open a migrated SQLite database on a tmp path.

    The repository layer only depends on the connection having
    applied the WAL contract (``state_machine.db.connection.open``)
    and the migration DDL (``state_machine.db.schema.migrate``).
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# Tests — insertion / conflict path
# ---------------------------------------------------------------------------


def test_upsert_inserts_new_row(tmp_path: Path) -> None:
    """``upsert`` on a fresh key inserts a new row.

    Verifies:
      * row count goes from 0 to 1
      * the inserted row carries the expected field values
    """
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    conn = _open_db(tmp_path)
    try:
        repo = ArtifactRepository(conn)
        repo.upsert(
            plan_id="p1",
            artifact_type="interview",
            file_path="/plans/p1/interview.json",
            status="generated",
            content_hash="abc123",
        )

        cur = conn.execute(
            "SELECT plan_id, artifact_type, file_path, status, content_hash "
            "FROM plan_artifacts WHERE plan_id = ?",
            ("p1",),
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        plan_id, artifact_type, file_path, status, content_hash = rows[0]
        assert plan_id == "p1"
        assert artifact_type == "interview"
        assert file_path == "/plans/p1/interview.json"
        assert status == "generated"
        assert content_hash == "abc123"
    finally:
        conn.close()


def test_upsert_updates_existing_row_on_conflict(tmp_path: Path) -> None:
    """Second ``upsert`` on the same key updates the existing row in place.

    The table uses ``(plan_id, artifact_type)`` as the primary key,
    so a duplicate upsert MUST NOT add a second row. The fields on the
    surviving row reflect the second call.
    """
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    conn = _open_db(tmp_path)
    try:
        repo = ArtifactRepository(conn)
        # First insert
        repo.upsert(
            plan_id="p1",
            artifact_type="prd",
            file_path="/old/prd.md",
            status="pending",
            content_hash=None,
        )
        # Second upsert — same key, different field values
        repo.upsert(
            plan_id="p1",
            artifact_type="prd",
            file_path="/new/prd.md",
            status="generated",
            content_hash="deadbeef",
        )

        cur = conn.execute(
            "SELECT plan_id, artifact_type, file_path, status, content_hash "
            "FROM plan_artifacts WHERE plan_id = ? AND artifact_type = ?",
            ("p1", "prd"),
        )
        rows = cur.fetchall()
        assert len(rows) == 1, (
            "upsert must update in place, but table has "
            f"{len(rows)} rows for (p1, prd)"
        )
        plan_id, artifact_type, file_path, status, content_hash = rows[0]
        assert plan_id == "p1"
        assert artifact_type == "prd"
        assert file_path == "/new/prd.md"
        assert status == "generated"
        assert content_hash == "deadbeef"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests — pointer-not-body anchor
# ---------------------------------------------------------------------------


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
    path pointing to the file *on disk* — the body itself is NOT
    mirrored into the database. We write a 10MB file under
    ``tmp_path``, then upsert a pointer to it, and assert the DB
    footprint grows by less than 1KB.

    The pathological case is that the table accidentally stored the
    file body (e.g. as a BLOB or as a base64-encoded string) — in
    that scenario the database would swell by ~10MB. The repository
    only persists the path string (a few hundred bytes) and an
    optional content hash, so the body must NOT show up in the
    ``plan_artifacts`` row.
    """
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    db_path = tmp_path / "state.db"
    body_path = tmp_path / "interview.json"
    body_path.write_bytes(b"x" * (10 * 1024 * 1024))  # 10MB body
    assert body_path.stat().st_size == 10 * 1024 * 1024

    conn = _open_db(tmp_path)
    try:
        repo = ArtifactRepository(conn)
        before = _db_total_size(db_path)

        # Upsert the *pointer* (the path on disk) only — the body
        # on disk is NOT read or stored anywhere.
        repo.upsert(
            plan_id="p1",
            artifact_type="interview",
            file_path=str(body_path),
            status="generated",
            content_hash="abc",
        )

        # Issue a checkpoint so the WAL contents are flushed into
        # the main DB file before the size snapshot. Without this
        # the WAL sidecar would still hold the pending write and
        # the "after" snapshot would either miss the row entirely
        # (depending on timing) or split the measurement across
        # two files. Checkpointing makes the assertion stable.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        after = _db_total_size(db_path)
        delta = after - before
        # Anchor: < 1KB of growth proves the body is NOT in the DB.
        # The body file itself is 10MB, but the DB only grew by the
        # pointer row + a few hundred bytes of metadata.
        assert delta < 1024, (
            f"upsert of a 10MB file pointer grew the DB footprint by "
            f"{delta} bytes; the body must not be stored in the table"
        )

        # Belt-and-suspenders: the row in the table must NOT
        # contain any of the body bytes. Cross-check by reading
        # the row back and confirming the file_path column is
        # exactly the path string we passed in (no body bytes
        # leaked into the path).
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


# ---------------------------------------------------------------------------
# Tests — status enum validation
# ---------------------------------------------------------------------------


def test_artifact_status_enum_rejects_unknown_value(tmp_path: Path) -> None:
    """``status`` outside the canonical enum raises ``ValueError``.

    The enum is ``{pending, generated, stale, missing}``. Any other
    value (e.g. ``"bogus"``) MUST raise ``ValueError`` *before* the
    SQL executes, so a typo never produces a corrupt row.
    """
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    conn = _open_db(tmp_path)
    try:
        repo = ArtifactRepository(conn)
        with pytest.raises(ValueError):
            repo.upsert(
                plan_id="p1",
                artifact_type="prd",
                file_path="/p1/prd.md",
                status="bogus",
                content_hash="abc",
            )
        # Confirm no row was written.
        cur = conn.execute("SELECT COUNT(*) FROM plan_artifacts")
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests — concurrent upsert
# ---------------------------------------------------------------------------


def test_concurrent_upsert_same_key_is_idempotent(tmp_path: Path) -> None:
    """8 threads racing on the same ``(plan_id, artifact_type)`` yield 1 row.

    The ``ON CONFLICT`` clause in the SQL must serialise the writers
    so the final table has exactly one row for the key, regardless
    of how many threads try to insert simultaneously.

    Implementation notes: SQLite connections are thread-local by
    default (``check_same_thread=True``), so each worker thread
    opens its own connection against the same on-disk database. The
    WAL journal mode + 5s busy timeout (set by ``open``) lets the
    concurrent writers serialise cleanly on the same primary key.

    The repository's ON CONFLICT clause is the convergence point:
    each writer either inserts a new row or updates the existing
    one in place, so the final row count is exactly 1 regardless
    of how many threads race.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    db_path = tmp_path / "state.db"

    # Bootstrap the schema on a single connection, then close it
    # so the worker threads don't fight over a held writer lock.
    setup_conn = open_db(db_path)
    try:
        migrate(setup_conn)
        # Force the WAL contents to the main DB so the workers
        # see a clean schema when they open their own connections.
        setup_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        setup_conn.close()

    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        # Each thread owns its own connection — the ``open_db``
        # helper enforces ``check_same_thread=True`` so a shared
        # connection would raise on the second thread. SQLite's
        # WAL mode + 5s busy_timeout serialises the writers at
        # the SQL level.
        try:
            tconn = open_db(db_path)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
            return
        try:
            try:
                ArtifactRepository(tconn).upsert(
                    plan_id="p1",
                    artifact_type="interview",
                    file_path=f"/p1/interview_{idx}.json",
                    status="generated",
                    content_hash=f"hash-{idx}",
                )
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
        finally:
            tconn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"upsert raised in worker threads: {errors!r}"

    # Verify the convergent state via a fresh read-only
    # connection so we never read from a worker-thread connection
    # (which is closed by now).
    read_conn = open_db(db_path)
    try:
        cur = read_conn.execute(
            "SELECT COUNT(*) FROM plan_artifacts "
            "WHERE plan_id = ? AND artifact_type = ?",
            ("p1", "interview"),
        )
        count = cur.fetchone()[0]
    finally:
        read_conn.close()

    assert count == 1, (
        f"8 concurrent upserts on the same key produced {count} rows; "
        "ON CONFLICT must converge to 1"
    )


# ---------------------------------------------------------------------------
# Tests — mark_stale / mark_missing
# ---------------------------------------------------------------------------


def test_mark_stale_updates_status_to_stale(tmp_path: Path) -> None:
    """``mark_stale`` flips the row's status to ``"stale"``.

    The other columns (``file_path``, ``content_hash``) are NOT
    touched — only the status changes. We assert that the new status
    is ``stale`` and the pre-existing fields are preserved.
    """
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    conn = _open_db(tmp_path)
    try:
        repo = ArtifactRepository(conn)
        repo.upsert(
            plan_id="p1",
            artifact_type="prd",
            file_path="/p1/prd.md",
            status="generated",
            content_hash="abc",
        )

        repo.mark_stale(plan_id="p1", artifact_type="prd")

        row = conn.execute(
            "SELECT plan_id, artifact_type, file_path, status, content_hash "
            "FROM plan_artifacts WHERE plan_id = ? AND artifact_type = ?",
            ("p1", "prd"),
        ).fetchone()
        assert row is not None
        plan_id, artifact_type, file_path, status, content_hash = row
        assert plan_id == "p1"
        assert artifact_type == "prd"
        assert file_path == "/p1/prd.md"
        assert status == "stale"
        assert content_hash == "abc"
    finally:
        conn.close()


def test_mark_missing_updates_status_to_missing(tmp_path: Path) -> None:
    """``mark_missing`` flips the row's status to ``"missing"``.

    Like ``mark_stale``, only the status column changes; the pointer
    and the hash are preserved.
    """
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    conn = _open_db(tmp_path)
    try:
        repo = ArtifactRepository(conn)
        repo.upsert(
            plan_id="p2",
            artifact_type="arch",
            file_path="/p2/arch.md",
            status="pending",
            content_hash=None,
        )

        repo.mark_missing(plan_id="p2", artifact_type="arch")

        row = conn.execute(
            "SELECT status, file_path, content_hash "
            "FROM plan_artifacts WHERE plan_id = ? AND artifact_type = ?",
            ("p2", "arch"),
        ).fetchone()
        assert row is not None
        status, file_path, content_hash = row
        assert status == "missing"
        assert file_path == "/p2/arch.md"
        assert content_hash is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests — read APIs
# ---------------------------------------------------------------------------


def test_list_for_plan_returns_empty_for_unknown_plan(tmp_path: Path) -> None:
    """``list_for_plan`` on a plan that has no artifacts returns ``[]``."""
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    conn = _open_db(tmp_path)
    try:
        repo = ArtifactRepository(conn)
        result = repo.list_for_plan("does-not-exist")
        assert result == []
    finally:
        conn.close()


def test_list_for_plan_returns_all_artifacts(tmp_path: Path) -> None:
    """``list_for_plan`` returns every artifact row for the plan.

    Each entry is a dict (or dict-like record) carrying the four
    persisted columns (``artifact_type``, ``file_path``, ``status``,
    ``content_hash``). We assert the entries match what was upserted.
    """
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    conn = _open_db(tmp_path)
    try:
        repo = ArtifactRepository(conn)
        repo.upsert(
            "p1", "interview", "/p1/interview.json", "generated", "h1"
        )
        repo.upsert(
            "p1", "prd", "/p1/prd.md", "generated", "h2"
        )
        repo.upsert(
            "p2", "interview", "/p2/interview.json", "pending", None
        )

        rows = repo.list_for_plan("p1")
        assert len(rows) == 2
        types = {row["artifact_type"] for row in rows}
        assert types == {"interview", "prd"}
        # Every row has the four expected keys.
        for row in rows:
            assert set(row.keys()) >= {
                "artifact_type", "file_path", "status", "content_hash"
            }
    finally:
        conn.close()


def test_list_for_plans_groups_by_plan_id(tmp_path: Path) -> None:
    """``list_for_plans(plan_ids)`` returns ``{plan_id: [rows]}``.

    Plans with no artifacts appear as an empty list. Plans that were
    not in the input do not appear in the result.
    """
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    conn = _open_db(tmp_path)
    try:
        repo = ArtifactRepository(conn)
        repo.upsert("p1", "interview", "/p1/int.json", "generated", "h1")
        repo.upsert("p1", "prd", "/p1/prd.md", "stale", "h2")
        repo.upsert("p2", "arch", "/p2/arch.md", "generated", "h3")

        out = repo.list_for_plans(["p1", "p2", "p3"])
        assert set(out.keys()) == {"p1", "p2", "p3"}
        assert len(out["p1"]) == 2
        assert len(out["p2"]) == 1
        assert out["p3"] == []
    finally:
        conn.close()


def test_find_returns_row_for_existing_key(tmp_path: Path) -> None:
    """``find`` returns a single record for an existing key."""
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    conn = _open_db(tmp_path)
    try:
        repo = ArtifactRepository(conn)
        repo.upsert(
            "p1", "prd", "/p1/prd.md", "generated", "deadbeef"
        )

        row = repo.find("p1", "prd")
        assert row is not None
        assert row["artifact_type"] == "prd"
        assert row["file_path"] == "/p1/prd.md"
        assert row["status"] == "generated"
        assert row["content_hash"] == "deadbeef"
    finally:
        conn.close()


def test_find_returns_none_for_missing_key(tmp_path: Path) -> None:
    """``find`` returns ``None`` for a key that does not exist."""
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    conn = _open_db(tmp_path)
    try:
        repo = ArtifactRepository(conn)
        assert repo.find("missing", "interview") is None
    finally:
        conn.close()
