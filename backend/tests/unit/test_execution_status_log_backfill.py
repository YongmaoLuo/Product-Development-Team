"""The status endpoint backfills its logs from the log file on disk.

The defect this pins
--------------------
``GET /api/execution/{plan_id}/status`` answered with an empty ``logs``
list for every plan whose run this process did not start.  The
in-process buffer (``_execution_state[plan_id]["logs"]``) is a deque
that is created on start and never written to, and the state restored
from ``state.db`` carries an empty deque as well — so the field was
empty for a running plan too: after a restart, in a second process, and
for a plan whose directory the boot scan never walked.

The activity was on disk all the same.  ``plans/{plan_id}/execution.log``
is where the execution logger writes every entry, and nothing read it.
The consequence was that the reader had nothing but ``started_at`` to
go on, and a run that was quietly progressing looked exactly like a run
that had been stuck since it began.

What is pinned
--------------
* A ``running`` row with an empty in-process buffer backfills the tail
  of that file, and every line comes back in the one shape a reader can
  parse: ``[<UTC stamp>] <body>``.
* The backfill never *invents* freshness.  A log whose last entry is
  three hours old is reported as three hours old: a reader that asks
  "is this run still making progress?" has to be able to trust the
  stamp, so a stale log has to stay stale.
* A terminal row does not backfill.  A finished run's answer is the
  answer it wrote down, not a log tail.
* A missing or unreadable log degrades to an empty list and still
  answers 200 — the endpoint renders a card, and a card must render.
* The in-process lines are the freshest view of the run, so when they
  exist they are the answer and the disk is not read at all.
* The backfill reads a bounded window — the last 200 lines — because
  the log of a long run grows without bound and this read happens on a
  path that renders a card.
* A plan directory that resolves OUTSIDE the configured plans root is
  not read: it belongs to another tree.

Hermeticity
-----------
The ``plan_execution`` row is seeded through the per-test
``PDT_STATE_DB_PATH`` database that ``server._state_db_path()``
resolves — never the operator's live ``state.db``.  The autouse
``isolated_plans_dir`` fixture points ``PLANS_DIR`` at the per-test
tree, so the log file this suite writes is the one the route reads.
The in-process registry is cleared before and after every case: it is
a module-level dict, and a sibling test's entry would otherwise be
answered as this test's run.
"""

from __future__ import annotations

import json
import os
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server


PLAN_ID = "20261028-status-log-backfill"

#: The shape a reader may parse: a bracket, an ISO-8601 stamp, a close
#: bracket.  The offset is part of the stamp — a reader cannot tell
#: which instant a naive string names — so it is captured here and
#: parsed by :func:`_stamp_of`.
_STAMP_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)"
)


@pytest.fixture(autouse=True)
def clean_registry():
    """Empty the in-process registry around every case.

    ``server._execution_state`` is a module-level dict that outlives a
    test, so an entry left behind by a sibling case would be answered
    as this case's run.  The locks go with it: a lock held for a plan
    this case never started would block the route.
    """
    server._execution_state.clear()
    server._execution_locks.clear()
    yield
    server._execution_state.clear()
    server._execution_locks.clear()


@pytest.fixture
def client() -> TestClient:
    """A client on the real application."""
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_dir(plan_id: str) -> Path:
    """The plan's directory under the (redirected) plans root."""
    plan_dir = server.PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


def _log_entry(
    moment: datetime,
    event: str = "task_completed",
    message: str = "task 3 done",
) -> str:
    """One line of ``execution.log``, exactly as the logger writes it.

    The logger stamps entries with ``datetime.utcnow().isoformat()`` —
    a naive string that means UTC by convention — and writes them as
    JSON, one per line.
    """
    return json.dumps(
        {
            "ts": moment.replace(tzinfo=None).isoformat(),
            "level": "INFO",
            "event": event,
            "message": message,
        }
    )


def _write_log(plan_dir: Path, lines: list[str]) -> Path:
    """Write ``lines`` to the plan's ``execution.log``."""
    log_path = plan_dir / "execution.log"
    log_path.write_text(
        "".join(f"{line}\n" for line in lines),
        encoding="utf-8",
    )
    return log_path


def _seed_execution_row(plan_id: str, **fields) -> None:
    """Insert/update a ``plan_execution`` row in the hermetic test DB.

    Written through the production repository so the row shape matches
    what the status route reads back via ``ExecutionRepository.summary``.
    """
    from server import _state_db_path
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = db_open(_state_db_path())
    try:
        migrate(conn)
        ExecutionRepository(conn).update_fields(
            plan_id,
            create_if_missing=True,
            **fields,
        )
    finally:
        conn.close()


def _stamp_of(line: str) -> datetime:
    """Parse the timestamp a normalised line carries."""
    match = _STAMP_RE.match(line)
    assert match is not None, f"line carries no parsable stamp: {line!r}"
    raw = match.group(1)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


# ---------------------------------------------------------------------------
# The backfill
# ---------------------------------------------------------------------------


def test_backfills_fresh_tail_after_restart(client):
    """A running plan with nothing in memory is answered from the log file.

    This is the restart case: the registry is empty, the row says the
    run is going, and the activity is on disk.  The answer must carry
    the log's tail, in a shape the reader can parse, with a stamp that
    says the run was still moving.
    """
    plan_dir = _plan_dir(PLAN_ID)
    now = datetime.now(timezone.utc)
    _write_log(
        plan_dir,
        [
            _log_entry(now - timedelta(minutes=5), event="task_started"),
            _log_entry(now - timedelta(seconds=30)),
        ],
    )
    _seed_execution_row(
        PLAN_ID,
        exec_status="running",
        exec_pid=os.getpid(),
        started_at=(now - timedelta(minutes=6)).isoformat(),
    )

    resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "running"

    logs = data["logs"]
    assert logs, "the running plan's log tail was not backfilled"

    last = logs[-1]
    assert _STAMP_RE.match(last), f"last line carries no parsable stamp: {last!r}"
    stamp = _stamp_of(last)
    assert stamp.tzinfo is not None, (
        f"the backfilled stamp carries no offset: {last!r} — a reader "
        "cannot tell which instant it names"
    )
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    assert age < 300, (
        f"the backfilled tail's last stamp is {age:.0f}s old; the log's "
        "own freshness was not carried over"
    )
    assert "task 3 done" in last, last


def test_stale_tail_is_not_refreshed(client):
    """A stale log is reported as stale — the backfill never fabricates.

    The negative control for the case above.  If the backfill stamped
    the lines with the moment of the read, then every running plan
    would look fresh for ever, and the reader asking "has this run made
    progress lately?" would be told yes by a run that died three hours
    ago.  The stamp in the log is evidence; it has to survive.
    """
    plan_dir = _plan_dir(PLAN_ID)
    now = datetime.now(timezone.utc)
    stale = now - timedelta(hours=3)
    _write_log(plan_dir, [_log_entry(stale)])
    _seed_execution_row(
        PLAN_ID,
        exec_status="running",
        exec_pid=os.getpid(),
        started_at=stale.isoformat(),
    )

    resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    logs = resp.json()["logs"]
    assert logs, "the running plan's log tail was not backfilled"

    age = (datetime.now(timezone.utc) - _stamp_of(logs[-1])).total_seconds()
    assert age > 3 * 3600 - 60, (
        f"the backfilled tail claims to be {age:.0f}s old, but the log's "
        "last entry was three hours old — the stamp was refreshed"
    )


def test_missing_log_file_returns_empty_logs(client):
    """A plan with no log file at all still answers 200 with no logs."""
    _plan_dir(PLAN_ID)
    _seed_execution_row(
        PLAN_ID,
        exec_status="running",
        exec_pid=os.getpid(),
    )

    resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    assert resp.json()["logs"] == []


def test_inline_memory_logs_are_normalized(client):
    """The in-process lines are the answer when there are any.

    They come back in the same shape as a line read from disk, so a
    reader cannot tell which source a line came from.
    """
    _plan_dir(PLAN_ID)
    server._execution_state[PLAN_ID] = {
        "status": "running",
        "logs": deque(
            ["[2026-10-28T10:35:13] [INFO] task_completed: task 3 done"],
            maxlen=200,
        ),
        "project_dir": "",
        "process": None,
        "started_at": "2026-10-28T10:30:00",
        "ended_at": None,
        "pid": os.getpid(),
        "stop_reason": None,
    }

    resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    logs = resp.json()["logs"]
    assert logs, "the in-process log lines were dropped"
    assert "+00:00" in logs[0], (
        f"the in-process line was not normalised to an explicit UTC "
        f"offset: {logs[0]!r}"
    )


def test_memory_logs_win_over_the_disk_tail(client):
    """When both are there, the in-process lines are the answer.

    They are the freshest view of the run; the file on disk may be
    seconds behind them.  So the disk is not read at all — which is
    only observable when the two hold different lines.
    """
    plan_dir = _plan_dir(PLAN_ID)
    _write_log(
        plan_dir,
        [_log_entry(datetime.now(timezone.utc), message="from disk")],
    )
    server._execution_state[PLAN_ID] = {
        "status": "running",
        "logs": deque(["[2026-10-28T10:35:13] [INFO] from memory"], maxlen=200),
        "project_dir": "",
        "process": None,
        "started_at": "2026-10-28T10:30:00",
        "ended_at": None,
        "pid": os.getpid(),
        "stop_reason": None,
    }

    resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    logs = resp.json()["logs"]
    assert len(logs) == 1, logs
    assert "from memory" in logs[0], (
        f"the answer was not made of the in-process lines: {logs[0]!r}"
    )
    assert "from disk" not in logs[0], (
        f"the disk tail was read although the in-process lines were "
        f"there: {logs[0]!r}"
    )


def test_backfill_is_bounded_to_the_tail_window(client):
    """The backfill reads a bounded window, not the whole file.

    A long run's log grows without bound and this read happens on a
    path that renders a card, so the answer carries the tail — the
    last 200 lines — and nothing more.
    """
    plan_dir = _plan_dir(PLAN_ID)
    now = datetime.now(timezone.utc)
    _write_log(
        plan_dir,
        [_log_entry(now, event=f"event_{i}") for i in range(300)],
    )
    _seed_execution_row(
        PLAN_ID,
        exec_status="running",
        exec_pid=os.getpid(),
    )

    resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    logs = resp.json()["logs"]
    assert len(logs) == 200, f"expected the last 200 lines, got {len(logs)}"
    # And it is the TAIL, not an arbitrary window: the last entry is
    # the one the writer wrote last.
    assert "event_299" in logs[-1], logs[-1]


def test_terminal_row_does_not_backfill(client):
    """A finished run is answered from its verdict, not from a log tail."""
    plan_dir = _plan_dir(PLAN_ID)
    now = datetime.now(timezone.utc)
    _write_log(plan_dir, [_log_entry(now - timedelta(seconds=30))])
    _seed_execution_row(
        PLAN_ID,
        exec_status="completed",
        started_at=(now - timedelta(minutes=1)).isoformat(),
    )

    resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "completed"
    assert data["logs"] == [], (
        "a terminal row backfilled its log tail — the answer for a "
        "finished run is the verdict it wrote down"
    )


def test_plan_dir_outside_plans_root_is_not_backfilled(client, tmp_path):
    """A plan directory that points out of the tree is not read.

    The plan id is a leaf under the configured plans root.  A leaf that
    resolves elsewhere belongs to another tree, and reading it would
    answer about a plans root this process was never asked about.
    """
    outside = tmp_path / "outside-tree"
    outside.mkdir()
    _write_log(
        outside,
        [_log_entry(datetime.now(timezone.utc) - timedelta(seconds=30))],
    )

    plans_dir = server.PLANS_DIR
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / PLAN_ID).symlink_to(outside, target_is_directory=True)

    _seed_execution_row(
        PLAN_ID,
        exec_status="running",
        exec_pid=os.getpid(),
    )

    resp = client.get(f"/api/execution/{PLAN_ID}/status")

    assert resp.status_code == 200, resp.text
    assert resp.json()["logs"] == [], (
        "a plan directory outside the plans root was read"
    )
