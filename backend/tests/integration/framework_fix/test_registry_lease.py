"""The merge lease -- one un-merged repair PR per repository.

What this file pins
-------------------
* a repository has ONE merge slot: the second repair plan that asks for
  it is refused, and the holder's row is left exactly as it was;
* the holder asking again is idempotent -- it still holds the slot;
* releasing the slot hands it to whoever asks next;
* releasing a slot somebody else holds is refused -- the slot is not
  freed behind the holder's back;
* releasing a slot nobody holds is refused;
* two repositories have two slots -- the lease is per repository, not
  global;
* the PR URL is recorded with the lease;
* two callers racing for the same repository produce exactly one
  holder: the unique index settles the race and the loser gets
  ``False``, not an exception.

Why these tests are *integration* tests
---------------------------------------
The lease is a row in the state database, and the exclusivity is
enforced by the database's primary key -- not by anything inside this
process.  A double would test the double.  Each test gets the shared
``state_db`` fixture: a migrated file inside that test's own
``tmp_path``, never the operator's live ``state.db``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List

import pytest

from framework_fix.registry import FrameworkFixRegistry


def _lease_rows(db_path: Path) -> List[Dict[str, Any]]:
    """Every merge-lease row, ordered by repository."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT repo, fix_plan_id, pr_url, acquired_at "
            "FROM framework_fix_merge_lease ORDER BY repo"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _events(db_path: Path) -> List[Dict[str, Any]]:
    """Every audit-chain row, oldest first."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event_type, fingerprint, fix_plan_id, payload_json "
            "FROM framework_fix_event ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _event_types(db_path: Path) -> List[str]:
    """The audit chain's event types, oldest first."""
    return [str(event["event_type"]) for event in _events(db_path)]


def test_lease_exclusive(state_db: Path) -> None:
    """A repository has one merge slot: the second caller is refused."""
    registry = FrameworkFixRegistry(db_path=state_db)

    assert registry.acquire_merge_lease("repo-a", "fix-1") is True
    assert registry.acquire_merge_lease("repo-a", "fix-2") is False

    rows = _lease_rows(state_db)
    assert [row["fix_plan_id"] for row in rows] == ["fix-1"], (
        "the loser must not overwrite the holder; got "
        f"{[row['fix_plan_id'] for row in rows]!r}"
    )


def test_lease_reentrant(state_db: Path) -> None:
    """The holder asking again keeps the slot -- and asks for nothing new."""
    registry = FrameworkFixRegistry(db_path=state_db)

    assert registry.acquire_merge_lease("repo-a", "fix-1") is True
    assert registry.acquire_merge_lease("repo-a", "fix-1") is True

    rows = _lease_rows(state_db)
    assert len(rows) == 1, (
        "acquiring twice is one lease, not two; got " f"{len(rows)} rows"
    )
    assert rows[0]["fix_plan_id"] == "fix-1"


def test_release_then_acquire(state_db: Path) -> None:
    """Releasing the slot hands it to the next caller."""
    registry = FrameworkFixRegistry(db_path=state_db)

    assert registry.acquire_merge_lease("repo-a", "fix-1") is True
    assert registry.release_merge_lease("repo-a", "fix-1") is True
    assert registry.acquire_merge_lease("repo-a", "fix-2") is True

    rows = _lease_rows(state_db)
    assert [row["fix_plan_id"] for row in rows] == ["fix-2"], (
        "the freed slot goes to the next caller; got "
        f"{[row['fix_plan_id'] for row in rows]!r}"
    )


def test_release_foreign(state_db: Path) -> None:
    """Releasing somebody else's lease is refused -- it frees nothing."""
    registry = FrameworkFixRegistry(db_path=state_db)

    assert registry.acquire_merge_lease("repo-a", "fix-1") is True
    assert registry.release_merge_lease("repo-a", "fix-2") is False

    rows = _lease_rows(state_db)
    assert [row["fix_plan_id"] for row in rows] == ["fix-1"], (
        "releasing a lease somebody else holds must not free the slot; "
        f"got {[row['fix_plan_id'] for row in rows]!r}"
    )


def test_release_without_lease(state_db: Path) -> None:
    """Releasing a slot nobody holds is refused, not an error."""
    registry = FrameworkFixRegistry(db_path=state_db)

    assert registry.release_merge_lease("repo-a", "fix-1") is False


def test_leases_are_per_repository(state_db: Path) -> None:
    """Two repositories have two slots -- the lease is not global."""
    registry = FrameworkFixRegistry(db_path=state_db)

    assert registry.acquire_merge_lease("repo-a", "fix-1") is True
    assert registry.acquire_merge_lease("repo-b", "fix-2") is True

    rows = _lease_rows(state_db)
    assert {row["repo"] for row in rows} == {"repo-a", "repo-b"}


def test_lease_records_pr_url(state_db: Path) -> None:
    """The lease carries the PR URL it was taken for."""
    registry = FrameworkFixRegistry(db_path=state_db)

    assert (
        registry.acquire_merge_lease(
            "repo-a", "fix-1", "https://github.com/org/repo-a/pull/1"
        )
        is True
    )

    rows = _lease_rows(state_db)
    assert rows[0]["pr_url"] == "https://github.com/org/repo-a/pull/1"
    assert rows[0]["fix_plan_id"] == "fix-1"


def test_concurrent_acquire_has_one_winner(state_db: Path) -> None:
    """Two callers racing for one repository: exactly one holds the slot."""
    first = FrameworkFixRegistry(db_path=state_db)
    second = FrameworkFixRegistry(db_path=state_db)

    results: List[bool] = []
    barrier = threading.Barrier(2)

    def grab(registry: FrameworkFixRegistry, fix_plan_id: str) -> None:
        barrier.wait(timeout=30)
        results.append(registry.acquire_merge_lease("repo-a", fix_plan_id))

    threads = [
        threading.Thread(target=grab, args=(first, "fix-1")),
        threading.Thread(target=grab, args=(second, "fix-2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(results) == [False, True], (
        "the repository's merge slot goes to exactly one caller; got "
        f"{results!r}"
    )
    assert len(_lease_rows(state_db)) == 1


def test_unique_index_settles_a_lost_race(
    state_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The holder appearing under us is a lost race, not a crash.

    The read and the write are one transaction, so in production the
    holder cannot appear between them -- but the unique index is the
    *authority* for the exclusivity, and losing the race to it must be
    answered with ``False``, not with an exception, and must not
    overwrite the holder.
    """
    registry = FrameworkFixRegistry(db_path=state_db)

    # Somebody else takes the slot behind our back.
    conn = sqlite3.connect(str(state_db))
    try:
        conn.execute(
            "INSERT INTO framework_fix_merge_lease "
            "(repo, fix_plan_id, pr_url, acquired_at) "
            "VALUES (?, ?, ?, ?)",
            ("repo-a", "fix-2", None, "2026-11-24T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    # ...and we do not see it.
    monkeypatch.setattr(registry, "_lease_row", lambda conn, repo: None)

    assert registry.acquire_merge_lease("repo-a", "fix-1") is False

    rows = _lease_rows(state_db)
    assert [row["fix_plan_id"] for row in rows] == ["fix-2"], (
        "the losing race must not overwrite the holder; got "
        f"{[row['fix_plan_id'] for row in rows]!r}"
    )


# ---------------------------------------------------------------------------
# The audit chain
# ---------------------------------------------------------------------------
# The lease is a state transition, and the schema's contract is that every
# state transition appends one row to ``framework_fix_event``.  Without
# that row the question "who took this repository's merge slot, and when"
# has no answer in the database -- only in the log of whichever process
# happened to be running.
def test_acquire_appends_the_audit_event(state_db: Path) -> None:
    """Taking the slot leaves one ``merge_lease_acquired`` event."""
    registry = FrameworkFixRegistry(db_path=state_db)

    assert (
        registry.acquire_merge_lease(
            "repo-a", "fix-1", "https://github.com/org/repo-a/pull/1"
        )
        is True
    )

    assert _event_types(state_db) == ["merge_lease_acquired"], (
        "the lease is a state transition and must be on the audit chain; "
        f"got {_event_types(state_db)!r}"
    )
    event = _events(state_db)[0]
    assert event["fix_plan_id"] == "fix-1"
    payload = json.loads(event["payload_json"])
    assert payload["repo"] == "repo-a"
    assert payload["fix_plan_id"] == "fix-1"
    assert payload["pr_url"] == "https://github.com/org/repo-a/pull/1"


def test_idempotent_acquire_does_not_repeat_the_acquisition(
    state_db: Path,
) -> None:
    """The holder asking again does not fake a second acquisition."""
    registry = FrameworkFixRegistry(db_path=state_db)

    assert registry.acquire_merge_lease("repo-a", "fix-1") is True
    assert registry.acquire_merge_lease("repo-a", "fix-1") is True

    assert _event_types(state_db) == ["merge_lease_acquired"], (
        "one lease is one acquisition; a re-entrant ask must not append a "
        f"second one; got {_event_types(state_db)!r}"
    )


def test_a_lost_race_writes_no_acquisition_event(state_db: Path) -> None:
    """The loser of the race did not take the slot, so it leaves no trace."""
    registry = FrameworkFixRegistry(db_path=state_db)

    assert registry.acquire_merge_lease("repo-a", "fix-1") is True
    assert registry.acquire_merge_lease("repo-a", "fix-2") is False

    assert _event_types(state_db) == ["merge_lease_acquired"], (
        "the plan that was refused must not be recorded as holding the "
        f"slot; got {_event_types(state_db)!r}"
    )


def test_release_appends_the_audit_event(state_db: Path) -> None:
    """Giving the slot back leaves one ``merge_lease_released`` event."""
    registry = FrameworkFixRegistry(db_path=state_db)

    registry.acquire_merge_lease("repo-a", "fix-1")
    assert registry.release_merge_lease("repo-a", "fix-1") is True

    assert _event_types(state_db) == [
        "merge_lease_acquired",
        "merge_lease_released",
    ], (
        "the release is a state transition too; got "
        f"{_event_types(state_db)!r}"
    )
    released = _events(state_db)[1]
    assert released["fix_plan_id"] == "fix-1"
    payload = json.loads(released["payload_json"])
    assert payload["repo"] == "repo-a"
    assert payload["fix_plan_id"] == "fix-1"


def test_releasing_a_lease_nobody_holds_writes_no_event(
    state_db: Path,
) -> None:
    """Nothing was released, so nothing is recorded as released."""
    registry = FrameworkFixRegistry(db_path=state_db)

    assert registry.release_merge_lease("repo-a", "fix-1") is False

    assert _event_types(state_db) == [], (
        "the audit chain must not record a release that did not happen; "
        f"got {_event_types(state_db)!r}"
    )
