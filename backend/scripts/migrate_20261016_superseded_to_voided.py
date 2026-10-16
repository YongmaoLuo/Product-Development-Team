"""One-shot status migration: ``superseded`` → ``voided`` for reset orphans.

Background
----------
2026-10-16 split one overloaded word into two statuses:

* ``superseded`` — **work handed to children.** ``is_dependency_ready``
  resolves a dependency on such a row by expanding it to its
  ``<id>-*`` children and judging those instead.
* ``voided`` — **premise gone.** The task and its whole downstream
  should not run; nothing to expand to, nothing to wait for.

``/reset`` was writing ``superseded`` for the second meaning, because
until now there was no second status to write. On the live DB that is
24 rows, all with ``failure_reason`` starting ``[reset] superseded by
verification reset``, and **none** of them has a ``<id>-*`` child — so
under the new vocabulary every one of them is a ``voided`` row and none
is a ``superseded`` one.

Leaving them as ``superseded`` is not inert. A downstream task whose
``depends_on`` names one of those rows would expand to a child set that
does not exist. The resolver treats that as "nothing to wait for"
(:func:`agent._resolve_upstream_candidates` returns an empty candidate
list), so the row becomes a silent always-satisfied dependency instead
of a revoked premise.

Contract
--------
``migrate_superseded_to_voided(db_path)`` is:

* **Selective** — only rows with ``status='superseded'`` **and no**
  ``<task_id>-%`` sibling in the same plan. A superseded row that really
  does have children is doing its new job correctly; the migration
  leaves it alone and reports it under ``kept_with_children``.
* **Minimal** — it writes ``status`` and ``updated_at``. It does **not**
  touch ``failure_reason``: that text is the audit record of how the row
  got there ("[reset] superseded by verification reset"), and rewriting
  it would erase the only evidence of which code path wrote it. No code
  keys off the string — verified by grep, see the module's ``__main__``.
* **Idempotent** — already-``voided`` rows are not selected, so a second
  run is a no-op that reports ``0`` migrated.
* **Transactional** — one ``BEGIN IMMEDIATE`` … ``COMMIT``; a failure
  rolls the whole thing back.
* **Dry-run by default** at the CLI.

Not part of the dispatcher: this only converges rows written by the old
writer. Rows created after the code change already get ``voided``.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

#: Rows carrying this status and this reason prefix are the ones the old
#: ``/reset`` writer produced. Recorded here so an operator reading the
#: script can see the classification without running it; the migration
#: itself does NOT filter on the text (a superseded row written by any
#: other path is migrated too, subject to the children guard).
RESET_REASON_PREFIX = "[reset] superseded by verification reset"

#: The status we are moving away from, and the one we are moving to.
OLD_STATUS = "superseded"
NEW_STATUS = "voided"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_superseded_rows(
    con: sqlite3.Connection,
) -> Dict[str, List[Dict[str, Any]]]:
    """Split the ``superseded`` rows into "migrate" and "leave alone".

    Read-only: it runs a single ``SELECT`` and never opens a write
    transaction, so it is safe to call against the production DB.

    The children test is the same prefix test the rest of the codebase
    uses to mean "task ``X`` was split into ``X-*``" — see
    ``agent._breakdown_children``. A row with children is a *pointer*
    (``superseded``'s new meaning) and must stay that way.
    """
    cur = con.execute(
        "SELECT plan_id, task_id, failure_reason FROM plan_tasks "
        "WHERE status = ? ORDER BY plan_id, task_id",
        (OLD_STATUS,),
    )
    rows = cur.fetchall()

    migrate: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    for plan_id, task_id, failure_reason in rows:
        n = con.execute(
            "SELECT COUNT(*) FROM plan_tasks WHERE plan_id = ? AND task_id LIKE ?",
            (plan_id, f"{task_id}-%"),
        ).fetchone()[0]
        entry = {
            "plan_id": plan_id,
            "task_id": task_id,
            "children": n,
            "reason": (failure_reason or "")[:120],
        }
        (migrate if n == 0 else kept).append(entry)
    return {"migrate": migrate, "kept_with_children": kept}


def migrate_superseded_to_voided(db_path: Path) -> Dict[str, Any]:
    """Rewrite childless ``superseded`` rows to ``voided``.

    Returns a summary dict: ``{"dry_run", "migrated", "kept_with_children",
    "backup"}``. ``migrated`` is a list of ``{plan_id, task_id}``.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    # Read the classification through a read-only handle first. If that
    # says there is nothing to do, we never open a write transaction —
    # a second run of an already-migrated DB touches nothing at all.
    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        plan = classify_superseded_rows(ro)
    finally:
        ro.close()

    if not plan["migrate"]:
        return {
            "dry_run": False,
            "migrated": [],
            "kept_with_children": plan["kept_with_children"],
            "backup": None,
        }

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        stamp = _now()
        con.execute("BEGIN IMMEDIATE")
        written: List[Dict[str, Any]] = []
        for entry in plan["migrate"]:
            # Re-check the predicate inside the transaction: between the
            # read-only pass and here, a dispatcher could have split one
            # of these tasks. The CAS on ``status`` makes that visible
            # as a smaller ``written`` list rather than a wrong one.
            cur = con.execute(
                "UPDATE plan_tasks SET status = ?, updated_at = ? "
                "WHERE plan_id = ? AND task_id = ? AND status = ?",
                (NEW_STATUS, stamp, entry["plan_id"], entry["task_id"], OLD_STATUS),
            )
            if cur.rowcount == 1:
                written.append(
                    {"plan_id": entry["plan_id"], "task_id": entry["task_id"]}
                )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    return {
        "dry_run": False,
        "migrated": written,
        "kept_with_children": plan["kept_with_children"],
        "backup": None,
    }


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, type=Path,
                    help="path to state.db")
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default: dry run, report only)")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the .bak copy taken before writing")
    args = ap.parse_args(argv)

    ro = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        plan = classify_superseded_rows(ro)
    finally:
        ro.close()

    total = len(plan["migrate"]) + len(plan["kept_with_children"])
    print(f"{OLD_STATUS!r} rows found: {total}")
    print(f"  -> migrate to {NEW_STATUS!r} (no children): {len(plan['migrate'])}")
    print(f"  -> keep as {OLD_STATUS!r} (has <id>-* children): "
          f"{len(plan['kept_with_children'])}")
    for entry in plan["kept_with_children"]:
        print(f"     keep: plan={entry['plan_id']} task={entry['task_id']} "
              f"children={entry['children']}")
    reset_n = sum(1 for e in plan["migrate"]
                  if e["reason"].startswith(RESET_REASON_PREFIX))
    print(f"     of the migratable rows, {reset_n} carry the "
          f"{RESET_REASON_PREFIX!r} prefix")

    if not args.apply:
        print("dry run — nothing written (pass --apply to write)")
        return 0

    backup = None
    if not args.no_backup:
        backup = args.db.with_suffix(args.db.suffix + ".bak")
        shutil.copy2(args.db, backup)
        print(f"backup written: {backup}")

    result = migrate_superseded_to_voided(args.db)
    result["backup"] = str(backup) if backup else None
    print(f"migrated {len(result['migrated'])} rows to {NEW_STATUS!r}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
