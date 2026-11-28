"""The framework-fix registry's core contract — registration, lookup, events.

What this file pins
-------------------
* ``register_hit`` records the hit, keeps **one** registry row per
  fingerprint, and returns that fingerprint;
* ``lookup`` answers "what is this defect's state right now?" — and
  answers "nothing" (``None``) for a fingerprint nobody registered;
* ``list_hits`` answers "how often, and in which plans, did this
  signature bite?";
* ``register_hit`` leaves a ``discovered`` event behind: the audit
  chain is append-only, and its first link is written in the same
  transaction as the rows it describes.

Why these tests are *integration* tests
---------------------------------------
The registry's entire job is to keep facts in a database that outlives
the process that produced them.  A registry test that fakes the
database would therefore test nothing that matters: the uniqueness
constraint, the transaction boundary and the migration are all
properties of the real SQLite file, not of a double.  The database
each test gets is the shared ``state_db`` fixture — a migrated file
inside that test's own ``tmp_path``, never the operator's live
``state.db``.

Three of the cases below exist to catch the registry being *too weak*:

* the duplicate case proves the uniqueness is real (one registry row,
  two hit rows) rather than the second registration quietly
  overwriting the first;
* the unmigrated case proves the registry refuses to invent its own
  schema — a database the production migration never touched must
  raise, not silently create four tables and look healthy;
* the atomicity case proves the write is **one** transaction: a
  failure after the registry row was written must leave nothing
  behind, because half a registration is worse than none.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from framework_fix.registry import (
    FrameworkFixRegistry,
    SchemaNotMigratedError,
)


#: Row counts, one literal query per table.  The table names are
#: literals rather than an interpolated fragment so that this file
#: never assembles SQL out of a variable.
_COUNT_QUERIES = {
    "framework_fix_registry": "SELECT COUNT(*) FROM framework_fix_registry",
    "framework_fix_hit": "SELECT COUNT(*) FROM framework_fix_hit",
    "framework_fix_event": "SELECT COUNT(*) FROM framework_fix_event",
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


def _events(db_path: Path) -> List[Dict[str, Any]]:
    """Every event row, oldest first."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event_id, fingerprint, fix_plan_id, event_type, "
            "payload_json, created_at FROM framework_fix_event "
            "ORDER BY created_at, event_id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def test_register_then_lookup(state_db: Path) -> None:
    """A registered hit is findable by its fingerprint."""
    registry = FrameworkFixRegistry(db_path=state_db)

    fingerprint = registry.register_hit(_hit())

    assert fingerprint.startswith("sha256:"), (
        f"the fingerprint must carry its algorithm; got {fingerprint!r}"
    )

    row = registry.lookup(fingerprint)
    assert row is not None, "a registered fingerprint must be findable"
    assert row["fingerprint"] == fingerprint
    assert row["status"] == "open", (
        f"a freshly registered defect is open; got {row['status']!r}"
    )
    assert row["fix_plan_id"] == "plan-a"
    assert row["key"]["error_class"] == "assertionerror"
    assert row["key"]["framework_files"] == [
        "backend/framework_fix/registry.py"
    ]

    hits = registry.list_hits(fingerprint)
    assert len(hits) == 1
    assert hits[0]["plan_id"] == "plan-a"
    assert hits[0]["vp_id"] == "VP-3"


def test_duplicate_hits(state_db: Path) -> None:
    """Two plans hitting the same signature: one registry row, two hits."""
    registry = FrameworkFixRegistry(db_path=state_db)

    first = registry.register_hit(_hit(plan_id="plan-a"))
    second = registry.register_hit(_hit(plan_id="plan-b", vp_id="VP-9"))

    assert first == second, (
        "the same failure material must produce the same fingerprint — "
        f"{first!r} != {second!r}"
    )
    assert _count(state_db, "framework_fix_registry") == 1, (
        "one signature is one defect: the registry row must stay unique"
    )
    assert _count(state_db, "framework_fix_hit") == 2, (
        "every hit is on record — the second plan's hit must not be lost"
    )

    hits = registry.list_hits(first)
    assert [hit["plan_id"] for hit in hits] == ["plan-a", "plan-b"]


def test_lookup_missing(state_db: Path) -> None:
    """An unregistered fingerprint has no registry row."""
    registry = FrameworkFixRegistry(db_path=state_db)

    assert registry.lookup("sha256:" + "0" * 64) is None
    assert registry.list_hits("sha256:" + "0" * 64) == []


def test_event_appended(state_db: Path) -> None:
    """Registering leaves a ``discovered`` event on the audit chain."""
    registry = FrameworkFixRegistry(db_path=state_db)

    fingerprint = registry.register_hit(_hit())

    events = _events(state_db)
    assert len(events) == 1, f"expected exactly one event, got {events!r}"
    event = events[0]
    assert event["event_type"] == "discovered"
    assert event["fingerprint"] == fingerprint
    payload = json.loads(event["payload_json"])
    assert payload["plan_id"] == "plan-a"
    assert payload["error_class"] == "AssertionError"


def test_append_event_appends_one_row(state_db: Path) -> None:
    """``append_event`` adds one row and says how many it added."""
    registry = FrameworkFixRegistry(db_path=state_db)
    fingerprint = registry.register_hit(_hit())

    appended = registry.append_event(
        "attached",
        {"plan_id": "plan-b"},
        fingerprint=fingerprint,
    )

    assert appended == 1
    events = _events(state_db)
    assert [event["event_type"] for event in events] == [
        "discovered",
        "attached",
    ]


def test_db_path_defaults_to_the_resolver(state_db: Path) -> None:
    """``db_path=None`` means "ask the application where the database is"."""
    registry = FrameworkFixRegistry()

    assert registry.db_path == state_db, (
        "the default must be the application's own resolver — "
        f"{registry.db_path} != {state_db}"
    )

    fingerprint = registry.register_hit(_hit())

    assert registry.lookup(fingerprint) is not None
    assert _count(state_db, "framework_fix_registry") == 1


def test_unmigrated_database_raises(tmp_path: Path) -> None:
    """A database without the migration is refused, not repaired."""
    bare = tmp_path / "bare.db"
    conn = sqlite3.connect(str(bare))
    conn.close()

    with pytest.raises(SchemaNotMigratedError) as excinfo:
        FrameworkFixRegistry(db_path=bare)

    assert "framework_fix_registry" in str(excinfo.value), (
        "the error must name what is missing; got "
        f"{str(excinfo.value)!r}"
    )

    conn = sqlite3.connect(str(bare))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()
    assert "framework_fix_registry" not in tables, (
        "the registry must not create its own schema: a database the "
        "production migration never touched has to stay untouched"
    )


def test_register_hit_is_one_transaction(
    state_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after the registry row is written rolls the whole write back."""
    registry = FrameworkFixRegistry(db_path=state_db)

    def boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("the event write failed")

    monkeypatch.setattr(registry, "_insert_event", boom)

    with pytest.raises(RuntimeError):
        registry.register_hit(_hit())

    assert _count(state_db, "framework_fix_registry") == 0, (
        "a half-written registration is worse than none — the registry "
        "row must not survive the rollback"
    )
    assert _count(state_db, "framework_fix_hit") == 0
    assert _count(state_db, "framework_fix_event") == 0


# ---------------------------------------------------------------------------
# Pointing a row at a repair plan
# ---------------------------------------------------------------------------
# The registry is the defect's only register, so "this row now belongs to
# that repair plan" is a write the registry owns.  Whoever wrote it with a
# bare ``UPDATE`` behind the registry's back would leave the audit chain
# without a link, and the row would answer "which plan fixes this?" with a
# value nobody can trace.
def test_link_fix_plan_points_the_row_and_audits_it(state_db: Path) -> None:
    """Linking a row moves it to the repair plan and says so."""
    registry = FrameworkFixRegistry(db_path=state_db)
    fingerprint = registry.register_hit(_hit())

    assert registry.link_fix_plan(fingerprint, "fix-1", repo="repo-a") is True

    row = registry.lookup(fingerprint)
    assert row is not None
    assert row["fix_plan_id"] == "fix-1", (
        "the row must point at the repair plan; got "
        f"{row['fix_plan_id']!r}"
    )
    assert row["repo"] == "repo-a", (
        "the repository is operational data the link carries; got "
        f"{row['repo']!r}"
    )

    events = _events(state_db)
    assert [event["event_type"] for event in events] == [
        "discovered",
        "fix_plan_linked",
    ], (
        "the link is a state transition and must be on the audit chain; "
        f"got {[event['event_type'] for event in events]!r}"
    )
    linked = events[1]
    assert linked["fingerprint"] == fingerprint
    assert linked["fix_plan_id"] == "fix-1"
    payload = json.loads(linked["payload_json"])
    assert payload["fix_plan_id"] == "fix-1"
    assert payload["repo"] == "repo-a"


def test_link_fix_plan_on_an_unregistered_fingerprint_writes_nothing(
    state_db: Path,
) -> None:
    """There is no row to point, so nothing is written and nothing claimed."""
    registry = FrameworkFixRegistry(db_path=state_db)

    assert registry.link_fix_plan("sha256:" + "0" * 64, "fix-1") is False

    assert _events(state_db) == [], (
        "a link that changed nothing must not be recorded; got "
        f"{_events(state_db)!r}"
    )


def test_link_fix_plan_keeps_a_known_repository(state_db: Path) -> None:
    """A repository that is already known is not overwritten by a later link."""
    registry = FrameworkFixRegistry(db_path=state_db)
    fingerprint = registry.register_hit(_hit(repo="repo-b"))

    registry.link_fix_plan(fingerprint, "fix-1", repo="repo-a")

    row = registry.lookup(fingerprint)
    assert row is not None
    assert row["repo"] == "repo-b", (
        "the repository a defect was first seen in is an operational fact; "
        f"a later link must not rewrite it; got {row['repo']!r}"
    )
