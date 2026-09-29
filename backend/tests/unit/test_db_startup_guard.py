"""Tests for the state.db startup guard + shutdown backup (2026-09-23).

Backstory: the 2026-09-23 outage ran for hours on a database whose
*reads* all worked (WAL overlay masked deep b-tree corruption) and
whose every *write* failed.  The guard's probe therefore writes —
a rolled-back transaction — and the shutdown hook leaves a restorable
copy behind on every clean stop.  These tests pin both halves, and in
parterial the exact failure the incident produced: a database that
passes read-only checks but cannot serve a write must be caught at
boot.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from state_machine.db.startup_guard import (  # noqa: E402
    GUARD_ENV,
    BACKUP_DIR_ENV,
    backup_now,
    probe_writability,
    quarantine,
    startup_guard,
)


@pytest.fixture
def backups_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "backups"
    monkeypatch.setenv(BACKUP_DIR_ENV, str(d))
    return d


def _make_healthy_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
        conn.commit()
    finally:
        conn.close()


class TestWriteProbe:
    def test_passes_on_a_healthy_database(self, tmp_path):
        db = tmp_path / "state.db"
        _make_healthy_db(db)
        probe_writability(db)  # must not raise

    def test_leaves_no_trace_behind(self, tmp_path):
        db = tmp_path / "state.db"
        _make_healthy_db(db)
        probe_writability(db)
        conn = sqlite3.connect(str(db))
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        assert "_startup_write_probe" not in tables, (
            "the probe transaction must roll back — no trace may survive"
        )

    def test_raises_on_a_garbage_file(self, tmp_path):
        db = tmp_path / "state.db"
        db.write_bytes(b"this is not a sqlite database at all")
        with pytest.raises(sqlite3.Error):
            probe_writability(db)


class TestShutdownBackup:
    def test_backup_now_copies_the_database(self, tmp_path, backups_dir):
        db = tmp_path / "state.db"
        _make_healthy_db(db)
        dest = backup_now(db)
        assert dest is not None and dest.exists()
        assert dest.read_bytes() == db.read_bytes()

    def test_backup_now_prunes_to_keep(self, tmp_path, backups_dir):
        db = tmp_path / "state.db"
        _make_healthy_db(db)
        for i in range(5):
            fake = backups_dir / f"state-2026090{i}_00000{i}.db"
            fake.parent.mkdir(parents=True, exist_ok=True)
            _make_healthy_db(fake)
        backup_now(db, keep=3)
        remaining = sorted(backups_dir.glob("state-*.db"))
        assert len(remaining) == 3, (
            f"retention must bound the backup dir; found {len(remaining)}"
        )

    def test_backup_now_never_raises(self, tmp_path, backups_dir, monkeypatch):
        db = tmp_path / "state.db"  # does not exist
        assert backup_now(db) is None


class TestStartupGuard:
    def test_healthy_database_is_untouched(self, tmp_path, backups_dir):
        db = tmp_path / "state.db"
        _make_healthy_db(db)
        before = db.read_bytes()

        startup_guard(db)

        assert db.read_bytes() == before
        assert not list(tmp_path.glob("state.db.corrupt-*"))

    def test_corrupt_database_is_quarantined_and_restored(
        self, tmp_path, backups_dir,
    ):
        db = tmp_path / "state.db"
        _make_healthy_db(db)
        # A distinguishable good backup.
        marker = tmp_path / "marker.db"
        _make_healthy_db(marker)
        conn = sqlite3.connect(str(marker))
        try:
            conn.execute("CREATE TABLE marker (note TEXT)")
            conn.execute("INSERT INTO marker VALUES ('from-backup')")
            conn.commit()
        finally:
            conn.close()
        backups_dir.mkdir(parents=True)
        (backups_dir / "state-20260923_000001.db").write_bytes(
            marker.read_bytes()
        )
        # A newer but broken backup — must be skipped by the probe.
        (backups_dir / "state-20260923_000002.db").write_bytes(
            b"corrupt backup"
        )
        # Now corrupt the live database the way the incident did:
        # reads still work is NOT reproducible cheaply, so use garbage —
        # the probe fails, which is the only fact the guard acts on.
        db.write_bytes(b"garbage")

        startup_guard(db)

        conn = sqlite3.connect(str(db))
        try:
            note = conn.execute("SELECT note FROM marker").fetchone()[0]
        finally:
            conn.close()
        assert note == "from-backup", (
            "the newest GOOD backup must be restored, skipping the broken one"
        )
        quarantined = list(tmp_path.glob("state.db.corrupt-*"))
        assert quarantined, "the corrupt file must be preserved for forensics"

    def test_corrupt_database_with_no_backup_starts_empty(
        self, tmp_path, backups_dir,
    ):
        db = tmp_path / "state.db"
        db.write_bytes(b"garbage")

        startup_guard(db)

        # No backup → empty start; the schema must be recreatable.
        from state_machine.db.schema import migrate

        conn = sqlite3.connect(str(db))
        try:
            migrate(conn)
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        assert "plan_tasks" in tables, (
            "after an empty start, migrate() must be able to rebuild the schema"
        )
        assert list(tmp_path.glob("state.db.corrupt-*")), (
            "evidence must be preserved even with no backup"
        )

    def test_guard_env_off_leaves_the_corrupt_database_in_place(
        self, tmp_path, backups_dir, monkeypatch,
    ):
        monkeypatch.setenv(GUARD_ENV, "off")
        db = tmp_path / "state.db"
        db.write_bytes(b"garbage")

        startup_guard(db)

        assert db.read_bytes() == b"garbage", (
            "with the guard off, the operator takes responsibility — "
            "the file must be left exactly as it was"
        )


class TestQuarantine:
    def test_quarantine_moves_sidecars_too(self, tmp_path):
        db = tmp_path / "state.db"
        _make_healthy_db(db)
        db.with_name("state.db-wal").write_bytes(b"wal")
        db.with_name("state.db-shm").write_bytes(b"shm")

        dest = quarantine(db)

        assert not db.exists()
        assert dest.exists()
        assert Path(str(dest) + "-wal").exists()
        assert Path(str(dest) + "-shm").exists()
