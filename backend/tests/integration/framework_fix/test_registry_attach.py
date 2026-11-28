"""The registry's attach relation — who waits on which fix, and how two
signatures become one defect.

What this file pins
-------------------
* ``attach`` records the plan / verification point / round / source that
  waits on a fix, and attaching the same ``(fingerprint, plan, vp)`` twice
  is **one** row with one id;
* attaching to a fingerprint nobody registered raises ``KeyError`` — a
  hold on a defect that does not exist holds nothing;
* the status of a hold follows the hit: a blocking hit holds the plan
  (``blocked``), a signature that was only registered does not
  (``registered``);
* ``merge_into`` writes the alias that makes the duplicate resolve to the
  canonical fingerprint, and keeps **both** sides' history;
* a merge into a target nobody registered **promotes** the target --
  the defect becomes visible under it, and the merged side's own row
  stays on record;
* a hold placed through an alias holds the canonical defect, and is the
  same hold as the one placed on the canonical fingerprint;
* ``attach_registered_hits`` attaches every plan whose hit only
  registered a signature -- including the plans waiting on a signature
  that was later merged into this one -- and does nothing for a
  fingerprint the registry does not know;
* merging a fingerprint into itself raises ``ValueError``.

Why these tests are *integration* tests
---------------------------------------
Same reason as the registry core: the attach relation lives in a SQLite
table that outlives the process, the alias resolution walks that table,
and the merge moves rows between tables.  All three are properties of the
real database, not of a double.  Each test gets the shared ``state_db``
fixture — a migrated file inside that test's own ``tmp_path``, never the
operator's live ``state.db``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from framework_fix.registry import (
    ATTACH_STATUS_BLOCKED,
    ATTACH_STATUS_EXHAUSTED,
    ATTACH_STATUS_REGISTERED,
    ATTACH_STATUSES,
    FrameworkFixRegistry,
)


#: Row counts, one literal query per table.  The table names are
#: literals rather than an interpolated fragment so that this file
#: never assembles SQL out of a variable.
_COUNT_QUERIES = {
    "framework_fix_registry": "SELECT COUNT(*) FROM framework_fix_registry",
    "framework_fix_hit": "SELECT COUNT(*) FROM framework_fix_hit",
    "framework_fix_attach": "SELECT COUNT(*) FROM framework_fix_attach",
    "framework_fix_alias": "SELECT COUNT(*) FROM framework_fix_alias",
}


def _hit(**overrides: Any) -> Dict[str, Any]:
    """A hit, as the registry's callers describe one."""
    hit: Dict[str, Any] = {
        "plan_id": "plan-a",
        "vp_id": "VP-3",
        "framework_files": ["backend/framework_fix/registry.py"],
        "error_class": "AssertionError",
        "first_error_line": "AssertionError: expected 1, got 2",
    }
    hit.update(overrides)
    return hit


def _count(db_path: Path, table: str) -> int:
    """How many rows a table holds — read through a plain connection."""
    query = _COUNT_QUERIES[table]
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(query).fetchone()[0])
    finally:
        conn.close()


def _attach_rows(db_path: Path) -> List[Dict[str, Any]]:
    """Every attach row, oldest first."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT attach_id, fingerprint, plan_id, vp_id, round, source, "
            "status, held_since FROM framework_fix_attach "
            "ORDER BY held_since, attach_id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _alias_rows(db_path: Path) -> List[Dict[str, Any]]:
    """Every alias row."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT alias_fingerprint, canonical_fingerprint "
            "FROM framework_fix_alias ORDER BY alias_fingerprint"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def test_attach_idempotent(state_db: Path) -> None:
    """The same (fingerprint, plan, vp) attached twice is one hold."""
    registry = FrameworkFixRegistry(db_path=state_db)
    fingerprint = registry.register_hit(_hit(plan_id="plan-a", vp_id="VP-3"))

    first = registry.attach(
        fingerprint, "plan-a", "VP-3", 2, "clean_main_repro"
    )
    second = registry.attach(
        fingerprint, "plan-a", "VP-3", 2, "clean_main_repro"
    )

    assert first == second, (
        "attaching the same verification point twice must not produce a "
        f"second hold: {first!r} != {second!r}"
    )
    rows = _attach_rows(state_db)
    assert len(rows) == 1, f"expected exactly one attach row, got {rows!r}"
    row = rows[0]
    assert row["attach_id"] == first
    assert row["fingerprint"] == fingerprint
    assert row["plan_id"] == "plan-a"
    assert row["vp_id"] == "VP-3"
    assert row["round"] == 2
    assert row["source"] == "clean_main_repro"


def test_attach_unknown_fp(state_db: Path) -> None:
    """A fingerprint nobody registered cannot be held on."""
    registry = FrameworkFixRegistry(db_path=state_db)

    with pytest.raises(KeyError):
        registry.attach(
            "sha256:" + "0" * 64, "plan-a", "VP-3", 1, "clean_main_repro"
        )

    assert _attach_rows(state_db) == [], (
        "a refused attach must leave no row behind — a hold on a defect "
        "that does not exist holds nothing"
    )


def test_attach_status_follows_the_hit(state_db: Path) -> None:
    """A blocking hit holds the plan; a registered signature does not."""
    registry = FrameworkFixRegistry(db_path=state_db)
    blocking = registry.register_hit(
        _hit(plan_id="plan-a", vp_id="VP-3", blocking=True)
    )
    registered = registry.register_hit(
        _hit(
            plan_id="plan-b",
            vp_id="VP-1",
            error_class="KeyError",
            first_error_line="KeyError: 'missing'",
        )
    )

    registry.attach(blocking, "plan-a", "VP-3", 1, "clean_main_repro")
    registry.attach(registered, "plan-b", "VP-1", 1, "clean_main_repro")

    statuses = {row["plan_id"]: row["status"] for row in _attach_rows(state_db)}
    assert statuses == {"plan-a": "blocked", "plan-b": "registered"}, (
        "the hold's status must follow the hit that caused it: a blocking "
        "hit holds the plan's execution, a signature that was only "
        f"registered only waits for the re-verification; got {statuses!r}"
    )


def test_attach_status_vocabulary_carries_exhausted() -> None:
    """The closed attach-status vocabulary can say "exhausted".

    The rounds of one signature ran out: the attachment stops holding the
    plan's execution, and a human decides what happens next.  The word
    has to be **in** the vocabulary — a status nobody can write is a
    status nobody can report.
    """
    assert ATTACH_STATUS_EXHAUSTED == "exhausted"
    assert ATTACH_STATUS_EXHAUSTED in ATTACH_STATUSES
    assert set(ATTACH_STATUSES) == {
        ATTACH_STATUS_BLOCKED,
        ATTACH_STATUS_REGISTERED,
        ATTACH_STATUS_EXHAUSTED,
    }, (
        "the attach-status vocabulary is a closed set — a new word here "
        f"means a new meaning; got {ATTACH_STATUSES!r}"
    )


def test_set_attach_status_keeps_the_records(state_db: Path) -> None:
    """Marking the attachments exhausted keeps the rows — the history stays.

    The attachment row answers "who ever waited for this repair".  An
    exhausted signature still has that history, so the row is **marked**,
    not deleted — and because it is no longer ``blocked``, the plan stops
    carrying a blocked attachment.
    """
    registry = FrameworkFixRegistry(db_path=state_db)
    fingerprint = registry.register_hit(
        _hit(plan_id="plan-a", vp_id="VP-3", blocking=True)
    )
    registry.attach(fingerprint, "plan-a", "VP-3", 2, "clean_main_repro")

    changed = registry.set_attach_status(fingerprint, ATTACH_STATUS_EXHAUSTED)

    assert changed == 1, f"one attachment was marked exhausted, got {changed}"
    rows = _attach_rows(state_db)
    assert len(rows) == 1, (
        f"the attachment record is history — it stays; got {rows!r}"
    )
    assert rows[0]["status"] == ATTACH_STATUS_EXHAUSTED, (
        f"the attachment must not block any more; got {rows[0]!r}"
    )


def test_set_attach_status_rejects_an_unknown_word(state_db: Path) -> None:
    """A word outside the closed vocabulary is a typo, not a new meaning."""
    registry = FrameworkFixRegistry(db_path=state_db)
    fingerprint = registry.register_hit(
        _hit(plan_id="plan-a", vp_id="VP-3", blocking=True)
    )
    registry.attach(fingerprint, "plan-a", "VP-3", 2, "clean_main_repro")

    with pytest.raises(ValueError):
        registry.set_attach_status(fingerprint, "exhaust")

    rows = _attach_rows(state_db)
    assert rows[0]["status"] == ATTACH_STATUS_BLOCKED, (
        "a refused status change leaves the row alone; got " f"{rows[0]!r}"
    )


def test_registry_writes_its_own_status(state_db: Path) -> None:
    """The registry row's status is written by the registry — and it sticks.

    "The repair was merged" and "the repair did not hold" are two answers
    the registry has to give, and the registry is the only place that may
    write them.
    """
    registry = FrameworkFixRegistry(db_path=state_db)
    fingerprint = registry.register_hit(_hit(plan_id="plan-a"))

    assert registry.mark_fixed(fingerprint) is True
    row = registry.lookup(fingerprint)
    assert row is not None and row["status"] == "fixed", (
        f"the merged repair fixes the defect; got {row!r}"
    )

    assert registry.reopen(fingerprint) is True
    row = registry.lookup(fingerprint)
    assert row is not None and row["status"] == "reopened", (
        f"a rerun that still fails reopens the defect; got {row!r}"
    )


def test_merge_into_alias(state_db: Path) -> None:
    """Merging writes the alias and keeps both sides' history."""
    registry = FrameworkFixRegistry(db_path=state_db)
    canonical = registry.register_hit(_hit(plan_id="plan-a"))
    duplicate = registry.register_hit(
        _hit(
            plan_id="plan-b",
            error_class="KeyError",
            first_error_line="KeyError: 'missing'",
        )
    )

    registry.merge_into(canonical, duplicate)

    resolved = registry.lookup(duplicate)
    assert resolved is not None, (
        "a merged fingerprint must still be findable — it now resolves to "
        "the canonical one"
    )
    assert resolved["fingerprint"] == canonical, (
        "the duplicate must resolve to the canonical fingerprint; got "
        f"{resolved['fingerprint']!r} != {canonical!r}"
    )

    aliases = _alias_rows(state_db)
    assert aliases == [
        {"alias_fingerprint": duplicate, "canonical_fingerprint": canonical}
    ], f"the merge must leave the alias on record; got {aliases!r}"

    hits = registry.list_hits(canonical)
    assert sorted(hit["plan_id"] for hit in hits) == ["plan-a", "plan-b"], (
        "a merge must not delete history: both sides' hits stay readable "
        f"under the canonical fingerprint; got {hits!r}"
    )
    assert _count(state_db, "framework_fix_registry") == 2, (
        "the duplicate's own registry row is history — a merge redirects "
        "the lookup, it does not erase the record"
    )
    assert _count(state_db, "framework_fix_hit") == 2


def test_attach_registered_hits(state_db: Path) -> None:
    """Every plan that only registered a signature is attached."""
    registry = FrameworkFixRegistry(db_path=state_db)
    fingerprint = registry.register_hit(_hit(plan_id="plan-a", vp_id="VP-1"))
    registry.register_hit(_hit(plan_id="plan-b", vp_id="VP-2"))
    registry.register_hit(_hit(plan_id="plan-c", vp_id="VP-3"))

    attached = registry.attach_registered_hits(fingerprint)

    assert attached == 3, (
        "the caller needs to know how many plans are waiting on this fix; "
        f"got {attached!r}"
    )
    rows = registry.list_attach(fingerprint)
    assert sorted(row["plan_id"] for row in rows) == [
        "plan-a",
        "plan-b",
        "plan-c",
    ], f"every plan that hit the signature must be attached; got {rows!r}"
    assert {row["status"] for row in rows} == {"registered"}, (
        "a signature that was only registered does not hold execution; got "
        f"{rows!r}"
    )


def test_attach_registered_hits_unknown_fp(state_db: Path) -> None:
    """A fingerprint the registry does not know is not attached to."""
    registry = FrameworkFixRegistry(db_path=state_db)

    attached = registry.attach_registered_hits("sha256:" + "0" * 64)

    assert attached == 0
    assert _attach_rows(state_db) == []


def test_merge_self_raises(state_db: Path) -> None:
    """A fingerprint cannot be merged into itself."""
    registry = FrameworkFixRegistry(db_path=state_db)
    fingerprint = registry.register_hit(_hit())

    with pytest.raises(ValueError):
        registry.merge_into(fingerprint, fingerprint)

    assert _alias_rows(state_db) == [], (
        "a refused merge must not leave an alias behind — a self-alias is "
        "a cycle, and a cycle has no chain end"
    )


def test_attach_registered_hits_covers_the_merged_family(state_db: Path) -> None:
    """A merged-away signature's waiting plans are attached as well.

    The semantic merge makes two signatures one defect, so the plans that
    only registered a hit on the merged-away signature are waiting on that
    same fix.  Leaving them out would drop them from the re-verification
    set after the fix lands -- the fix would land and nobody would notice
    it had not fixed their plan.
    """
    registry = FrameworkFixRegistry(db_path=state_db)
    canonical = registry.register_hit(_hit(plan_id="plan-a", vp_id="VP-1"))
    duplicate = registry.register_hit(
        _hit(
            plan_id="plan-b",
            vp_id="VP-2",
            error_class="KeyError",
            first_error_line="KeyError: 'missing'",
        )
    )
    registry.write_alias(duplicate, canonical)

    attached = registry.attach_registered_hits(canonical)

    assert attached == 2, (
        "both signatures' waiting plans wait on one defect now; got "
        f"{attached!r}"
    )
    rows = registry.list_attach(canonical)
    assert sorted(row["plan_id"] for row in rows) == ["plan-a", "plan-b"], (
        f"every plan waiting on the merged defect must be attached; got {rows!r}"
    )
    assert {row["status"] for row in rows} == {"registered"}, (
        "a signature that was only registered does not hold execution; got "
        f"{rows!r}"
    )


def test_merge_into_promotes_the_target(state_db: Path) -> None:
    """Merging into a target nobody registered makes the target the defect.

    The other merge test registers *both* sides, so it never reaches the
    case this one pins: the target has no registry row of its own.  There
    the defect's state has to become visible **under the target** -- that is
    what "the target becomes fp1" means -- while the merged side's own row
    stays on record as history.
    """
    registry = FrameworkFixRegistry(db_path=state_db)
    duplicate = registry.register_hit(
        _hit(
            plan_id="plan-b",
            error_class="KeyError",
            first_error_line="KeyError: 'missing'",
        )
    )
    target = "sha256:" + "1" * 64
    assert registry.lookup(target) is None, (
        "the target must start out unregistered -- otherwise this test "
        "proves nothing about the promotion"
    )

    registry.merge_into(target, duplicate)

    promoted = registry.lookup(target)
    assert promoted is not None, (
        "the defect must be visible under the target after the merge -- "
        "the target is where the earlier fix plan continues"
    )
    assert promoted["fingerprint"] == target
    assert promoted["fix_plan_id"] == "plan-b", (
        "the promoted state must carry the defect's own facts; got "
        f"{promoted!r}"
    )
    assert registry.lookup(duplicate)["fingerprint"] == target, (
        "the merged fingerprint must resolve to the target"
    )
    assert [hit["plan_id"] for hit in registry.list_hits(target)] == ["plan-b"], (
        "the merged side's history must stay readable under the target; got "
        f"{registry.list_hits(target)!r}"
    )
    assert _alias_rows(state_db) == [
        {"alias_fingerprint": duplicate, "canonical_fingerprint": target}
    ], f"the merge must leave the alias on record; got {_alias_rows(state_db)!r}"
    assert _count(state_db, "framework_fix_registry") == 2, (
        "the merged side's own registry row is history -- a merge adds the "
        "target's row, it does not erase the record"
    )


def test_attach_through_alias_lands_on_the_canonical(state_db: Path) -> None:
    """A hold placed through an alias holds the canonical defect.

    This is the point of the semantic merge: after two signatures are judged
    to be one defect, a plan that hits the *merged-away* signature is still
    waiting on the same fix.  Two holds for one defect would make the
    blocking count wrong, so the alias must resolve to the canonical row and
    the second hold must be the same hold.
    """
    registry = FrameworkFixRegistry(db_path=state_db)
    canonical = registry.register_hit(_hit(plan_id="plan-a"))
    duplicate = registry.register_hit(
        _hit(
            plan_id="plan-b",
            error_class="KeyError",
            first_error_line="KeyError: 'missing'",
        )
    )
    registry.write_alias(duplicate, canonical)

    through_alias = registry.attach(
        duplicate, "plan-b", "VP-1", 1, "clean_main_repro"
    )
    direct = registry.attach(
        canonical, "plan-b", "VP-1", 1, "clean_main_repro"
    )

    assert through_alias == direct, (
        "one verification point waiting on one defect is one hold, however "
        f"the fingerprint was spelled: {through_alias!r} != {direct!r}"
    )
    rows = registry.list_attach(canonical)
    assert len(rows) == 1, f"expected exactly one hold, got {rows!r}"
    assert rows[0]["attach_id"] == through_alias
    assert rows[0]["fingerprint"] == canonical, (
        "the hold must be recorded under the canonical fingerprint -- the "
        f"alias is a spelling, not a second defect; got {rows[0]!r}"
    )


def test_write_alias_resolves(state_db: Path) -> None:
    """A written alias makes the alias resolve to the canonical."""
    registry = FrameworkFixRegistry(db_path=state_db)
    canonical = registry.register_hit(_hit(plan_id="plan-a"))
    duplicate = registry.register_hit(
        _hit(
            plan_id="plan-b",
            error_class="KeyError",
            first_error_line="KeyError: 'missing'",
        )
    )

    registry.write_alias(duplicate, canonical)

    resolved = registry.lookup(duplicate)
    assert resolved is not None
    assert resolved["fingerprint"] == canonical
    assert _alias_rows(state_db) == [
        {"alias_fingerprint": duplicate, "canonical_fingerprint": canonical}
    ]
