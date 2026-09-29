"""Startup integrity guard + shutdown backup for the state database.

Backstory (2026-09-23)
----------------------
The backend server once ran for hours with a **deeply corrupt** ``state.db``
while every read kept working: the WAL overlay masked b-tree damage
(freelist corruption, duplicate child references) that made every
*write* fail with ``database disk image is malformed``.  ``PRAGMA
integrity_check`` reported "ok" the whole time because it only reads.
The corruption was only discovered when the last connection closed and
the WAL folded into the main file — by which point there was no going
back.  Root cause was an FD leak (connections GC-finalised in arbitrary
threads tearing WAL writes), but the operational lesson stands on its
own:

  **a server must not boot onto a database whose write path is broken,
  and a clean shutdown must leave a restorable copy behind.**

This module provides the two halves:

* :func:`startup_guard` — runs at server boot.  One rolled-back write
  probe (reads are not enough — see above).  On failure it quarantines
  the corrupt file *with* its ``-wal``/``-shm`` siblings for forensics
  and restores the newest backup that passes the same probe.  No usable
  backup → start empty (``migrate()`` recreates the schema; plans
  rebuild from their on-disk files) with a CRITICAL log recording where
  the evidence lives.

* :func:`backup_now` — runs on every clean shutdown (after a WAL
  checkpoint, so the copy is self-contained).  Retention is bounded so
  the backup directory cannot grow without limit.

Both are defensive by construction: neither raises into the caller, and
the guard can be disabled with ``PDT_DB_STARTUP_GUARD=off``.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Kill-switch for the auto-restore behaviour (operator escape hatch).
GUARD_ENV = "PDT_DB_STARTUP_GUARD"

#: Override for the backup directory; default is a ``backups/state-db``
#: folder next to the database itself.
BACKUP_DIR_ENV = "PDT_DB_BACKUP_DIR"

#: How many shutdown backups to retain.
DEFAULT_KEEP = 10


def probe_writability(db_path: Path) -> None:
    """Raise ``sqlite3.Error`` if the database cannot serve a write.

    Reads can succeed on a database whose write path is broken — that is
    exactly what the 2026-09-23 corruption looked like.  So the probe
    MUST write: a tiny transaction that is rolled back leaves no trace
    but exercises WAL append + commit + rollback.

    Parameters
    ----------
    db_path:
        Path to a SQLite database file.

    Raises
    ------
    sqlite3.Error
        Any SQLite failure: not a database, locked, malformed, I/O.
    """
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _startup_write_probe (x INTEGER)"
        )
        conn.execute("INSERT INTO _startup_write_probe VALUES (1)")
        conn.execute("DROP TABLE _startup_write_probe")
        conn.rollback()
    finally:
        conn.close()


def backup_dir_for(db_path: Path) -> Path:
    """Resolve the backup directory for ``db_path``."""
    override = os.environ.get(BACKUP_DIR_ENV)
    if override:
        return Path(override)
    return db_path.parent / "backups" / "state-db"


def backup_now(db_path: Path, keep: int = DEFAULT_KEEP) -> Optional[Path]:
    """Copy ``db_path`` into the backup directory, pruning to ``keep``.

    Called on every clean shutdown so a restart always has a recent
    good copy to restore from.  Never raises — a failed backup must not
    abort shutdown.

    Returns the new backup path, or ``None`` on any failure.
    """
    try:
        if not db_path.exists():
            return None
        bdir = backup_dir_for(db_path)
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = bdir / f"state-{stamp}.db"
        shutil.copy2(str(db_path), str(dest))
        backups = sorted(bdir.glob("state-*.db"))
        if len(backups) > keep:
            for old in backups[: len(backups) - keep]:
                old.unlink()
        return dest
    except Exception:  # noqa: BLE001 - backup must never break shutdown
        logger.exception("state.db shutdown backup failed")
        return None


def _newest_good_backup(db_path: Path) -> Optional[Path]:
    """Newest backup that passes the write probe, else ``None``."""
    bdir = backup_dir_for(db_path)
    if not bdir.exists():
        return None
    for cand in sorted(bdir.glob("state-*.db"), reverse=True):
        try:
            probe_writability(cand)
            return cand
        except sqlite3.Error:
            logger.warning("backup %s failed write probe, skipping", cand)
            continue
    return None


def quarantine(db_path: Path) -> Path:
    """Move ``db_path`` and its WAL siblings aside; return the new path.

    The sidecar files are moved too — they belong to the same
    (potentially corrupt) generation and must not be replayed against a
    replacement main file.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = db_path.with_name(f"{db_path.name}.corrupt-{stamp}")
    shutil.move(str(db_path), str(dest))
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            shutil.move(str(side), str(dest) + suffix)
    return dest


def startup_guard(db_path: Path) -> None:
    """Verify ``db_path`` at server boot; quarantine + restore if broken.

    Idempotent and cheap on the good path: a single rolled-back write
    probe.  On failure the corrupt file is preserved for forensics and
    the newest good backup restored in its place; with no usable backup
    the server starts from an empty database (``migrate()`` recreates
    the schema, plans rebuild from their on-disk directories) and a
    CRITICAL log records where the evidence lives.

    Set ``PDT_DB_STARTUP_GUARD=off`` to disable restoration (the probe
    still runs and still logs, but a corrupt database is left in place).
    """
    if os.environ.get(GUARD_ENV, "").lower() in ("0", "off", "false"):
        logger.warning(
            "startup guard restoration disabled via %s=%r",
            GUARD_ENV, os.environ.get(GUARD_ENV),
        )
        return
    if not db_path.exists():
        return
    try:
        probe_writability(db_path)
        return
    except sqlite3.Error as exc:
        logger.critical(
            "state.db FAILED the write probe (%s: %s) — quarantining and "
            "restoring from the newest good backup",
            type(exc).__name__, exc,
        )

    quarantined = quarantine(db_path)
    backup = _newest_good_backup(db_path)
    if backup is not None:
        shutil.copy2(str(backup), str(db_path))
        logger.critical(
            "state.db restored from %s — corrupt file preserved at %s",
            backup, quarantined,
        )
    else:
        logger.critical(
            "no usable backup found — starting with an EMPTY state.db; "
            "corrupt file preserved at %s",
            quarantined,
        )
