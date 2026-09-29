"""content_hash must be recomputed on update (VP-021, half B).

After ``upsert`` is called a second time with a different
``content_hash``, the row in ``plan_artifacts`` must reflect the
new hash. The status enum stays in the canonical four values
(``pending``, ``generated``, ``stale``, ``missing``). The DB row
count stays at 1 (no duplicates). DB size grows by a constant
amount regardless of body size. Concurrent upserts on the same
``(plan_id, artifact_type)`` converge to exactly one row.

The verification command from the verification plan is::

    pytest tests/unit/repositories/test_artifact_content_hash_recomputed_on_update.py -v
"""

from __future__ import annotations

import threading
from pathlib import Path

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.artifact_repository import (
    ArtifactRepository,
    VALID_STATUSES,
)


def _open_db(tmp_path: Path):
    """Open a migrated SQLite database on a tmp path."""
    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    return conn


def test_content_hash_recomputed_on_update(tmp_path: Path) -> None:
    """Second upsert updates content_hash in place.

    The ``(plan_id, artifact_type)`` row is preserved; only the
    pointer fields (``file_path``, ``content_hash``) and the
    ``status`` flip. We also confirm the status enum stays in
    the canonical set.
    """
    conn = _open_db(tmp_path)
    try:
        repo = ArtifactRepository(conn)
        repo.upsert(
            plan_id="p1",
            artifact_type="prd",
            file_path="/old/prd.md",
            status="pending",
            content_hash="hash-old",
        )
        repo.upsert(
            plan_id="p1",
            artifact_type="prd",
            file_path="/new/prd.md",
            status="generated",
            content_hash="hash-new",
        )

        cur = conn.execute(
            "SELECT plan_id, artifact_type, file_path, status, "
            "content_hash FROM plan_artifacts "
            "WHERE plan_id = ? AND artifact_type = ?",
            ("p1", "prd"),
        )
        rows = cur.fetchall()
        assert len(rows) == 1, (
            f"upsert must update in place; got {len(rows)} rows"
        )
        _, _, file_path, status, content_hash = rows[0]
        assert file_path == "/new/prd.md"
        assert status == "generated"
        assert content_hash == "hash-new", (
            "content_hash was not recomputed on update; the second "
            "upsert must overwrite the previous hash so the DB row "
            "reflects the latest on-disk body."
        )
        assert status in VALID_STATUSES
    finally:
        conn.close()


def test_status_enum_only_accepts_four_values(tmp_path: Path) -> None:
    """``status`` outside the canonical enum raises ``ValueError``.

    The canonical enum is ``{pending, generated, stale, missing}``.
    Any other value (e.g. ``"bogus"``) MUST raise ``ValueError``
    BEFORE the SQL executes.
    """
    import pytest

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
        cur = conn.execute("SELECT COUNT(*) FROM plan_artifacts")
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_db_growth_constant_per_upsert(tmp_path: Path) -> None:
    """DB increment is constant regardless of artifact body size.

    We upsert three rows with growing bodies (1KB, 100KB, 10MB)
    and assert that the largest-body upsert grows the DB by far
    less than the body size itself (i.e. the body is NOT in the
    DB). We rely on the main DB file only, with explicit
    checkpointing so WAL contents are flushed before each
    measurement.
    """
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    repo = ArtifactRepository(_open_db(tmp_path))

    sizes = [1024, 100 * 1024, 10 * 1024 * 1024]
    deltas = []

    for i, size in enumerate(sizes):
        body_path = tmp_path / f"body_{i}.bin"
        body_path.write_bytes(b"x" * size)

        # Checkpoint and measure BEFORE the upsert.
        _open_db(tmp_path).execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = db_path.stat().st_size

        repo.upsert(
            plan_id=f"p{i}",
            artifact_type="interview",
            file_path=str(body_path),
            status="generated",
            content_hash=f"hash-{i}",
        )

        # Checkpoint and measure AFTER the upsert.
        _open_db(tmp_path).execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = db_path.stat().st_size
        deltas.append(after - before)

    # All three upserts grew the DB by a small constant. The
    # 10MB-body upsert MUST NOT have grown the DB by anywhere
    # near 10MB - that would mean the body was mirrored.
    max_delta = max(deltas)
    assert max_delta < 4096, (
        f"upsert grew the DB by {max_delta} bytes; this is the "
        f"pointer-not-body regression: the body must not be "
        f"mirrored into plan_artifacts."
    )


def test_concurrent_upsert_same_key_converges_to_one_row(tmp_path: Path) -> None:
    """Concurrent upserts on the same key converge to exactly 1 row.

    The ``ON CONFLICT`` clause in the SQL must serialise the
    writers so the final table has exactly one row.
    """
    db_path = tmp_path / "state.db"

    setup_conn = open_db(db_path)
    try:
        migrate(setup_conn)
        setup_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        setup_conn.close()

    errors: list = []

    def worker(idx: int) -> None:
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
        "ON CONFLICT must converge to 1."
    )
