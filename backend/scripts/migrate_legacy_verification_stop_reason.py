"""One-shot migration: carry a legacy mirror stop reason into the authoritative row.

Background
----------
Before the verification writes moved to ``plan_verification`` (arch decision
point 1), the only home of a verification loop's stop reason was the
``plan_routing.verification`` mirror column — the pre-2026-11-04
``backend/verification_loop.py`` wrote it there with a bare ``UPDATE plan_routing
SET verification = …``.  A plan that stopped its verification loop back then
has **no** authoritative row, so the reason would be lost the moment the reads
stop consulting the column — and the "first reason wins" rule (the reason of
the first stop IS the cause) would start overwriting the real cause of the
termination.

Carrying the reason over as a *read-path fallback* is what went wrong
(VP-015 (c)): the read wrote a row, so the same plan answered differently
depending on the order the readers ran in, and the mirror column became a
state source again.  The carry-over is therefore an **explicit,
operator-triggered** one-time migration, and the read path only ever reads
the authoritative row.

Contract
--------
``migrate_legacy_verification_stop_reasons(db_path, apply=False)`` is:

* **Selective** — only plans whose ``plan_routing.verification`` mirror
  column carries a ``stop_reason`` *and* which have no ``plan_verification``
  row yet.  A plan that already has an authoritative row owns the newer
  record; the migration leaves it alone and reports it under
  ``skipped_existing_row``.
* **Minimal** — it carries the **reason only**.  The mirror's ``status`` /
  ``round`` / ``max_rounds`` are NOT a source (arch decision point 3): the
  row is created with the documented defaults — ``status="not_started"``,
  ``round=0``, ``max_rounds=DEFAULT_MAX_VERIFICATION_ROUNDS``.  The plan's
  phase is not consulted either: the routing phase would be a second
  source of the verification status.
* **Idempotent** — a second run finds nothing to carry (the migrated plans
  now have an authoritative row) and reports ``0`` migrated.
* **Atomic per plan** — each migrated plan's authoritative row is written
  through the repository's own narrow write
  (``VerificationRepository.set_state``) inside one ``BEGIN IMMEDIATE`` …
  ``COMMIT``.  A plan is either fully migrated or not migrated at all; a
  failure on one plan is reported and the run continues with the next one.
* **Dry-run by default** at the CLI — ``--apply`` writes.
* **Read-only classification** — :func:`classify_legacy_stop_reasons` runs
  a couple of ``SELECT``s and never opens a write transaction.

Not part of the dispatcher: this only converges rows written by the old
writer.  Plans created after the code change already get their verification
state in ``plan_verification``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# The migration lives under ``backend/scripts/``; ``plan_state`` lives one
# level up.  Putting the backend directory on the path makes the script
# runnable as ``python3 backend/scripts/migrate_legacy_verification_stop_reason.py``
# as well as importable as ``scripts.migrate_legacy_verification_stop_reason``.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_state import migrate_mirror_stop_reason  # noqa: E402

#: Stable identifier for this migration.  It is not recorded in a plan's
#: audit trail (the authoritative row itself is the record), but it is
#: printed by the CLI so an operator can tell which migration ran.
MIGRATION_ID = "20261114_legacy_verification_stop_reason"


def _decode_mirror(raw: Any) -> Dict[str, Any]:
    """Decode the mirror column.  A corrupted blob means "nothing to carry"."""
    if raw is None:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def classify_legacy_stop_reasons(
    con: sqlite3.Connection,
) -> Dict[str, List[Dict[str, Any]]]:
    """Split the plans into "migrate" and "leave alone".

    Read-only: it runs a couple of ``SELECT``s and never opens a write
    transaction, so it is safe to call against the production DB.

    "Migrate" means: the retired mirror column carries a stop reason and
    the authoritative table says nothing about this plan yet.  "Leave
    alone" means: the plan already has an authoritative row — that row is
    the newer record and wins.
    """
    rows = con.execute(
        "SELECT plan_id, current_phase, verification FROM plan_routing "
        "WHERE verification IS NOT NULL AND verification != '' "
        "ORDER BY plan_id"
    ).fetchall()

    migrate: List[Dict[str, Any]] = []
    skipped_existing_row: List[Dict[str, Any]] = []
    for plan_id, current_phase, verification in rows:
        stop_reason = _decode_mirror(verification).get("stop_reason")
        if not stop_reason:
            # Nothing to carry — the column never held a reason for this
            # plan (or held one that was never written).
            continue
        has_row = (
            con.execute(
                "SELECT 1 FROM plan_verification WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            is not None
        )
        entry = {
            "plan_id": plan_id,
            "stop_reason": str(stop_reason),
            "current_phase": current_phase,
        }
        (skipped_existing_row if has_row else migrate).append(entry)
    return {"migrate": migrate, "skipped_existing_row": skipped_existing_row}


def migrate_legacy_verification_stop_reasons(
    db_path: Path,
    apply: bool = False,
) -> Dict[str, Any]:
    """Carry the legacy mirror stop reasons into the authoritative rows.

    Args:
        db_path: the state database to migrate.
        apply: ``False`` (the default) classifies and reports without
            writing anything; ``True`` performs the carry-over.

    Returns a report dict: ``{"dry_run", "candidates", "migrated",
    "skipped_existing_row", "failed"}``.  ``candidates`` is what the run
    would carry over; ``migrated`` is what it actually carried over.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate as migrate_schema

    conn = open_db(db_path)
    try:
        migrate_schema(conn)
        plan = classify_legacy_stop_reasons(conn)
        migrated: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        if apply:
            for entry in plan["migrate"]:
                routing_row = {
                    "plan_id": entry["plan_id"],
                    "current_phase": entry["current_phase"],
                    "verification": json.dumps(
                        {"stop_reason": entry["stop_reason"]}
                    ),
                }
                row = migrate_mirror_stop_reason(
                    entry["plan_id"], routing_row, conn=conn
                )
                if row is None:
                    failed.append(
                        {
                            "plan_id": entry["plan_id"],
                            "error": "the authoritative row could not be written",
                        }
                    )
                    continue
                migrated.append(
                    {
                        "plan_id": entry["plan_id"],
                        "stop_reason": entry["stop_reason"],
                    }
                )
    finally:
        conn.close()

    return {
        "dry_run": not apply,
        "candidates": plan["migrate"],
        "migrated": migrated,
        "skipped_existing_row": plan["skipped_existing_row"],
        "failed": failed,
    }


def _backup(db_path: Path) -> Optional[Path]:
    """Copy the database next to itself before anything is written."""
    from state_machine.db.connection import open as open_db

    # Checkpoint the WAL first: a plain file copy of a WAL database can
    # miss the pages that still live in the WAL file.
    conn = open_db(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    backup = db_path.with_suffix(db_path.suffix + ".bak")
    shutil.copy2(db_path, backup)
    return backup


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, type=Path,
                    help="path to state.db")
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default: dry run, report only)")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the .bak copy taken before writing")
    args = ap.parse_args(argv)

    print(f"migration: {MIGRATION_ID}")
    report = migrate_legacy_verification_stop_reasons(args.db, apply=False)
    for entry in report["candidates"]:
        print(f"candidate: plan={entry['plan_id']} "
              f"stop_reason={entry['stop_reason']!r}")
    print(f"candidates: {len(report['candidates'])}, left alone "
          f"(already have an authoritative row): "
          f"{len(report['skipped_existing_row'])}")

    if not args.apply:
        print("dry run — nothing written (pass --apply to write)")
        return 0

    backup = None
    if not args.no_backup:
        backup = _backup(args.db)
        print(f"backup written: {backup}")

    result = migrate_legacy_verification_stop_reasons(args.db, apply=True)
    for entry in result["migrated"]:
        print(f"migrated: plan={entry['plan_id']} "
              f"stop_reason={entry['stop_reason']!r}")
    for entry in result["failed"]:
        print(f"FAILED: plan={entry['plan_id']} error={entry['error']}")
    print(f"migrated {len(result['migrated'])} plan(s), "
          f"{len(result['failed'])} failed, "
          f"{len(result['skipped_existing_row'])} left alone")
    return 1 if result["failed"] else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
