"""Unit tests: the scheduler's "stopping" view and the open-command list.

Background
----------
The scheduler answers the operator's questions from one read model, and
one of those questions is *"what is stopping?"*.  A plan that is asked to
pause leaves the running line **before** it comes back — the stop is a
request the worker has not answered yet — and a plan that is in that gap
is in neither the running line nor the queue.  An operator who cannot
see the gap cannot tell "the pause worked" from "the pause was lost".

So the read model grows a **stopping** list: one row per command the
scheduler has **accepted and not answered yet**.  The source of those
rows is the command log itself — the row is written the moment the
command is taken, with the outcome it has *so far* (``accepted``), and
it is turned into ``applied`` / ``failed`` only when the worker has
answered.  A command that is still ``accepted`` is therefore a command
that is still in flight.

The other half of this file is the **review fields on a waiting row**:
the finishing stage's waits carry the source they were asked under and
the pull request they are about, and the read model used to drop both
on the floor.  The rows that answer "what is waiting for me" are built
from them, so a reader that cannot see them cannot tell one waiting
plan from another.

What this file asserts
----------------------
1. **The stopping row is a closed shape** — four keys, no more.
2. **The document always carries the key** — an empty scheduler still
   answers ``stopping: []``, never a missing key.
3. **A broken timestamp keeps the row** — a row whose ``created_at``
   cannot be read is a row the operator still has to see.
4. **Only the open commands are listed** — a command that has been
   answered is not "stopping" any more.
5. **The review fields are passed on** — as they were written, and as
   empty values when the record does not carry them.

The store is read against a real SQLite database, because the contract
is a *storage* contract: what is listed is what is in the table, not
what a mock was told to expect.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from plan_scheduler.snapshot import _stopping_view, build
from plan_scheduler.store import PENDING_OUTCOME, SqliteSchedulerStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_migrated(db_path: Path) -> sqlite3.Connection:
    """Open a connection to a database that has the scheduler's tables."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    migrate(conn)
    return conn


@pytest.fixture()
def conn(tmp_path: Path):
    """A migrated connection, closed when the test is over."""
    connection = _open_migrated(tmp_path / "state.db")
    try:
        yield connection
    finally:
        connection.close()


#: The moment the open command was taken, and the clock the view is read
#: against.  The two are exactly two seconds apart, so the age the view
#: reports is exactly two seconds — a number that is read, not rounded.
_TAKEN_AT = "2026-11-20T08:42:56Z"
_NOW = 1795164178.0


def _command_row(
    plan_id: str = "p1",
    action: str = "pause",
    created_at: Optional[str] = _TAKEN_AT,
) -> Dict[str, Any]:
    """A row of the command log, as the store reads it back."""
    return {
        "plan_id": plan_id,
        "action": action,
        "outcome": PENDING_OUTCOME,
        "detail": None,
        "created_at": created_at,
    }


def _wait_row(**overrides: Any) -> Dict[str, Any]:
    """A wait record, as the finishing stage's registry writes it."""
    row: Dict[str, Any] = {
        "plan_id": "p1",
        "item_id": "i1",
        "wait_type": "approval",
        "question": "合入 PR #42？",
        "deadline": "2026-11-20T09:00:00Z",
        "extensions": [],
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 1. The stopping row is a closed shape
# ---------------------------------------------------------------------------


def test_stopping_view_shape() -> None:
    """A stopping row carries exactly the four keys the panel draws.

    The row names the plan and the action, says when the command was
    taken and how long ago that was.  A fifth key would be a field the
    panel cannot render, so the shape is closed here as firmly as the
    document's own.
    """
    view = _stopping_view([_command_row()], now=_NOW)

    assert len(view) == 1
    assert set(view[0]) == {"plan_id", "action", "since", "age_seconds"}
    assert view[0]["plan_id"] == "p1"
    assert view[0]["action"] == "pause"
    assert view[0]["since"] == _TAKEN_AT
    assert view[0]["age_seconds"] == 2.0


# ---------------------------------------------------------------------------
# 2. The document always carries the key
# ---------------------------------------------------------------------------


def test_build_includes_stopping_key() -> None:
    """An empty scheduler still answers ``stopping: []``.

    The key is not optional: a caller that asks "is anything stopping?"
    has to be able to ask it without first checking whether the answer
    is there.  A missing key would be a second answer to that question.
    """
    result = build({})

    assert "stopping" in result
    assert result["stopping"] == []


def test_build_reads_the_open_commands_from_the_state() -> None:
    """The state carries the open commands; the document names them.

    The state is the tick's own reading of the world, and the open
    commands are part of it — the engine asks the store for them and
    hands them over.  A state that carries none is an empty list.
    """
    result = build({"stopping": [_command_row(plan_id="p2", action="stop")]})

    assert len(result["stopping"]) == 1
    assert result["stopping"][0]["plan_id"] == "p2"
    assert result["stopping"][0]["action"] == "stop"


def test_two_commands_for_one_plan_are_two_rows() -> None:
    """Two open commands for one plan are two rows, not one.

    The commands are two facts, and a reader that merged them would
    lose one of them — the panel would show one stop where the operator
    asked for two.
    """
    rows = [
        _command_row(action="pause"),
        _command_row(action="resume"),
    ]

    view = _stopping_view(rows, now=_NOW)

    assert len(view) == 2
    assert [row["action"] for row in view] == ["pause", "resume"]


# ---------------------------------------------------------------------------
# 3. A broken timestamp keeps the row
# ---------------------------------------------------------------------------


def test_bad_timestamp_keeps_row() -> None:
    """A timestamp that cannot be read costs the age, not the row.

    The operator has to see that the plan is stopping even when the
    moment it was taken cannot be read — dropping the row would hide
    the very thing the row exists to show.  The age answers "not known"
    and the rest of the row is still there.
    """
    view = _stopping_view([_command_row(created_at="x")], now=_NOW)

    assert len(view) == 1
    assert view[0]["age_seconds"] is None
    assert view[0]["plan_id"] == "p1"
    assert view[0]["action"] == "pause"
    assert set(view[0]) == {"plan_id", "action", "since", "age_seconds"}


# ---------------------------------------------------------------------------
# 4. Only the open commands are listed
# ---------------------------------------------------------------------------


def test_list_open_commands_only_accepted(conn: sqlite3.Connection) -> None:
    """A command that has been answered is not stopping any more.

    The command log records three kinds of row: one that has been
    accepted and not answered yet, one that was carried out and one
    that failed.  Only the first is a plan that is still stopping, so
    only the first is listed — the other two are history.
    """
    store = SqliteSchedulerStore()
    store.record_command(conn, "p1", "pause", outcome="applied")
    store.record_command(conn, "p2", "resume", outcome="failed")
    store.record_command(conn, "p3", "stop", outcome=PENDING_OUTCOME)

    rows = store.list_open_commands(conn)

    assert len(rows) == 1
    assert rows[0]["plan_id"] == "p3"
    assert rows[0]["action"] == "stop"
    assert rows[0]["outcome"] == "accepted"
    assert rows[0]["created_at"]


def test_list_open_commands_is_empty_when_nothing_is_open(
    conn: sqlite3.Connection,
) -> None:
    """Nothing accepted and left open is an empty list, never ``None``."""
    store = SqliteSchedulerStore()
    store.record_command(conn, "p1", "pause", outcome="applied")

    assert store.list_open_commands(conn) == []


def test_list_open_commands_keeps_every_open_row(
    conn: sqlite3.Connection,
) -> None:
    """Two open commands are two rows, and the older one comes first."""
    store = SqliteSchedulerStore()
    store.record_command(conn, "p1", "pause", now="2026-11-20T08:42:56Z")
    store.record_command(conn, "p1", "resume", now="2026-11-20T08:43:56Z")

    rows = store.list_open_commands(conn)

    assert [row["action"] for row in rows] == ["pause", "resume"]


# ---------------------------------------------------------------------------
# 5. The review fields are passed on
# ---------------------------------------------------------------------------


def test_waiting_view_passes_review_keys() -> None:
    """The waiting row carries the source and the pull request it is about.

    The finishing stage's waits are asked under a source and are about a
    pull request, and the rows that answer "what is waiting for me" are
    built from both.  A record that carries them passes them on as they
    were written; a record that carries neither answers the empty
    source and no pull request — and it never raises for the missing
    key.
    """
    state: Dict[str, Any] = {
        "waits": [
            _wait_row(
                source="postprocess_approval",
                pr_number=42,
                pr_url="https://example.test/pr/42",
            )
        ]
    }

    row = build(state)["waiting"][0]

    assert row["source"] == "postprocess_approval"
    assert row["pr_number"] == 42
    assert row["pr_url"] == "https://example.test/pr/42"


def test_waiting_view_defaults_the_missing_review_keys() -> None:
    """A wait that names no source and no pull request answers empty.

    The keys are part of the row's shape, so a record written before
    they existed still produces a row the reader can read: the source
    is the empty string and the pull request is unknown.
    """
    row = build({"waits": [_wait_row()]})["waiting"][0]

    assert row["source"] == ""
    assert row["pr_number"] is None
    assert row["pr_url"] is None


def test_waiting_row_keeps_its_other_keys() -> None:
    """The three new keys are added; the eight before them do not move.

    The waiting row is a closed shape and the panel draws it — so the
    keys that were there are still there, in the same order, and the
    three new ones sit beside them.
    """
    row = build({"waits": [_wait_row()]})["waiting"][0]

    assert list(row) == [
        "plan_id",
        "item_id",
        "wait_type",
        "question",
        "deadline",
        "remaining_seconds",
        "extendable",
        "extensions_count",
        "source",
        "pr_number",
        "pr_url",
    ]
