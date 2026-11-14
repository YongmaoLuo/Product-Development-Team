"""Six base-drift blocks in a row must not spend the plan's round budget.

Why this gate exists
--------------------
Requirement A.4: a plan whose branch keeps drifting away from its base
must never end up reporting ``max_rounds_reached``, and every one of
those blocks must leave an auditable trace behind.

A round the base-drift check blocked dispatched **nothing**: the branch
had drifted away from its base, merging the base back left conflicts,
and the round therefore has no verdict at all — nobody ever looked at
the product.  It is not a failed round; it is a round that did not
happen.  ``max_rounds`` is the plan's own limit on how much work it may
do, so a round that did no work must not be charged to it.  Before the
base-drift accounting was fixed, four drifts were enough for a plan to
report ``max_rounds_reached`` after four rounds that never ran — and
the plan was no further from finished than when it started.

The three pieces that make that true were pinned one at a time:

* task 1 — ``mark_round_blocked``: the round row records WHY the round
  was blocked, without moving the round's status or its number;
* task 2 — ``_plan_next_round``: a round blocked before it dispatched
  anything is re-run under the SAME number, and is never ``exhausted``;
* task 3 — ``_record_base_drift_round_block``: the hand-off writes that
  row AND one audit event into the plan's own ``execution.log``.

This file is the end-to-end statement of requirement A.4: drive all
three in the order the production code drives them — six times in a row
— and check that the plan is still on round 1 with its budget intact,
and that six blocks left six events behind.

What is pinned here
-------------------
1. Six consecutive blocks leave the plan on round 1 — the round number
   never moves.
2. No pass is ever reported as ``exhausted`` — the budget is intact
   after every one of the six blocks.
3. Every block leaves exactly one audit event, and the six events name
   the six rounds they belong to.
4. A round that REALLY ran still advances: a failed verdict sends the
   plan to round 2.  The base-drift exception must not have turned the
   planner into "never advance".
5. The extreme budget — ``max_rounds = 1`` — still survives a block:
   a plan blocked on its only round is not out of budget, because that
   round never ran.

The database here is a REAL SQLite file under ``tmp_path``, reached
through ``PDT_STATE_DB_PATH`` — the repository is never mocked, so
these cases exercise the same writes, the same clearing and the same
budget arithmetic the running backend does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ``backend/`` on sys.path so ``import server`` works no matter which
# entry point runs the suite, and the repository root so the
# ``backend.framework`` package resolves.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent
for _path in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import server  # noqa: E402,F401  — imported first: server wires the loop
import verification_loop  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.verification_repository import (  # noqa: E402
    VerificationRepository,
)


PLAN_ID = "20261113-base-drift-budget"

#: The repair task the conflict becomes.
REPAIR_TASK_ID = "__base_drift__"

#: The plan's budget in the main scenario.
MAX_ROUNDS = 4

#: How many blocks the main scenario drives.
BLOCKS = 6

#: The stop reason a base-drift-blocked round carries.
STOP_REASON = verification_loop.BASE_DRIFT_CONFLICTS_STOP_REASON

#: The event a blocked round leaves in the plan's own execution log.
BLOCK_EVENT = verification_loop.BASE_DRIFT_ROUND_BLOCKED_EVENT

#: The conflict's own evidence, as the loop hands it over.
BLOCKED = {
    "status": STOP_REASON,
    "base_ref": "origin/main",
    "conflict_files": [{"path": "backend/a.py"}],
}


# ---------------------------------------------------------------------------
# Fixtures — a throwaway state.db and a throwaway plans tree
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """A throwaway ``state.db`` with one running round for ``PLAN_ID``.

    The env var is the seam: ``server._state_db_path()`` reads
    ``PDT_STATE_DB_PATH`` on every call, so the recorder under test
    writes into THIS file and not into the operator's live database.
    """
    path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(path))
    conn = open_db(path)
    try:
        migrate(conn)
        VerificationRepository(conn).insert(
            PLAN_ID, "running", round=1, max_rounds=MAX_ROUNDS,
        )
    finally:
        conn.close()
    return path


@pytest.fixture
def plans_dir(tmp_path, monkeypatch):
    """A throwaway plans tree holding the plan's own directory."""
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    # ``server._plan_dir`` resolves against the module attribute, which
    # was computed at import time — so it has to be patched.
    monkeypatch.setattr(server, "PLANS_DIR", root)
    # The execution logger resolves the plans root at construction
    # time through the env var, so the environment has to agree with the
    # patched attribute — otherwise the event lands in the live tree.
    monkeypatch.setenv("PDT_PLANS_DIR", str(root))
    (root / PLAN_ID).mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# Helpers — the same reads and writes the production code does
# ---------------------------------------------------------------------------


def _current(state_db):
    """The plan's verification row, read back through the repository."""
    conn = open_db(state_db)
    try:
        return VerificationRepository(conn).current(PLAN_ID)
    finally:
        conn.close()


def _init_round(state_db, round_n, max_rounds):
    """Start (or re-start) the plan's round under ``round_n``."""
    conn = open_db(state_db)
    try:
        VerificationRepository(conn).init_round(
            PLAN_ID, round_n, max_rounds,
        )
    finally:
        conn.close()


def _record_block(index):
    """The hand-off, exactly as the two production call sites make it."""
    return verification_loop._record_base_drift_round_block(
        PLAN_ID,
        f"round-token-{index}",
        BLOCKED,
        repair_task_id=REPAIR_TASK_ID,
    )


def _drive_blocks(state_db, rounds=BLOCKS):
    """Drive the production hand-off ``rounds`` times and collect the passes.

    The order is the production order:

    1. the hand-off records the block — it writes the round row and the
       audit event;
    2. the round planner reads the row the hand-off left and decides
       which round comes next;
    3. the round is re-initialised under the number the planner chose.

    Step 2 reads the row BEFORE step 3, and that order matters:
    ``init_round`` clears ``verification_stop_reason`` and ``results``,
    so a read taken after it would see a row that no longer says the
    round was blocked — and the planner would advance it.
    """
    passes = []
    for index in range(1, rounds + 1):
        _record_block(index)
        row = _current(state_db)
        next_round, cap, exhausted = verification_loop._plan_next_round(row)
        passes.append(
            {
                "row": row,
                "next_round": next_round,
                "max_rounds": cap,
                "exhausted": exhausted,
            }
        )
        _init_round(state_db, next_round, cap)
    return passes


def _audit_events(plans_dir):
    """The blocked-round events in the plan's own ``execution.log``.

    Counted by the event's own name, read out of the structured log
    lines — not by grepping the file for a word — so a line that merely
    mentions the event (in a message, in another event's data) is not
    counted, and the setup's own lines are not counted either.
    """
    log = plans_dir / PLAN_ID / "execution.log"
    if not log.exists():
        return []
    events = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("event") == BLOCK_EVENT:
            events.append(entry)
    return events


def _write_failed_verdict(state_db):
    """The row a round that REALLY ran leaves behind: a failed verdict."""
    conn = open_db(state_db)
    try:
        VerificationRepository(conn).complete_round(
            PLAN_ID,
            {
                "status": "failed",
                "stop_reason": "max_rounds_reached",
                "recorded_by": "_persist_verification_terminal",
            },
            status="failed",
            stop_reason="max_rounds_reached",
            publish_closed=False,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 1 — six consecutive blocks stay in round one
# ---------------------------------------------------------------------------


def test_six_consecutive_blocks_stay_in_round_one(state_db, plans_dir):
    """Six drifts in a row leave the plan on the round it started on.

    Each block dispatched nothing, so each one is re-run under the
    number it was blocked at.  The counter does not move, and the plan
    is still on round 1 after the sixth block.
    """
    passes = _drive_blocks(state_db)

    assert len(passes) == BLOCKS
    assert [entry["row"]["round"] for entry in passes] == [1] * BLOCKS
    assert [entry["next_round"] for entry in passes] == [1] * BLOCKS
    # The row each pass planned against really was the blocked row —
    # the hand-off's trace was on it, and the round was still running
    # (a blocked round is not a finished one).
    assert all(
        entry["row"]["verification_stop_reason"] == STOP_REASON
        for entry in passes
    )
    assert all(
        entry["row"]["verification_status"] == "running"
        for entry in passes
    )
    # The row the plan carries after the sixth block is round 1 too —
    # and the re-init cleared the block's trace without moving the
    # round with it.
    final = _current(state_db)
    assert final["round"] == 1
    assert final["verification_stop_reason"] is None
    assert final["results"] is None


# ---------------------------------------------------------------------------
# Test 2 — the budget is never exhausted
# ---------------------------------------------------------------------------


def test_never_reports_exhausted(state_db, plans_dir):
    """No block is ever reported as ``max_rounds_reached``.

    ``exhausted`` is the flag the caller turns into the
    ``max_rounds_reached`` verdict.  It must stay False across every
    block — otherwise six drifts are enough to kill a plan that never
    ran a single verification point.
    """
    passes = _drive_blocks(state_db)

    assert [entry["exhausted"] for entry in passes] == [False] * BLOCKS
    # The cap itself is never raised either: the plan's budget is the
    # budget it was set up with.
    assert [entry["max_rounds"] for entry in passes] == [MAX_ROUNDS] * BLOCKS


# ---------------------------------------------------------------------------
# Test 3 — every block leaves one auditable event
# ---------------------------------------------------------------------------


def test_each_block_leaves_one_audit_event(state_db, plans_dir):
    """Six blocks leave six events, and each names its own round.

    The row says WHY the round stopped; the event says so in the log an
    operator actually reads.  One block, one event — a block that left
    no trace is indistinguishable from a block that never happened.
    """
    _drive_blocks(state_db)

    events = _audit_events(plans_dir)
    assert len(events) == BLOCKS
    assert [entry["data"]["round_token"] for entry in events] == [
        f"round-token-{index}" for index in range(1, BLOCKS + 1)
    ]
    # Every event names the repair the block became, so the trail says
    # what happened next and not merely that something did.
    assert all(
        entry["data"]["base_drift_repair_task_id"] == REPAIR_TASK_ID
        for entry in events
    )


# ---------------------------------------------------------------------------
# Test 4 — a round that really ran still advances
# ---------------------------------------------------------------------------


def test_a_normal_verdict_still_advances(state_db, plans_dir):
    """The base-drift exception did not turn the planner into "never advance".

    A blocked round reuses its number.  A round that produced a verdict
    spent budget and advances as before — after six blocks the plan is
    on round 1, and the failed verdict sends it to round 2.
    """
    _drive_blocks(state_db)

    _write_failed_verdict(state_db)

    row = _current(state_db)
    assert row["verification_status"] == "failed"
    next_round, cap, exhausted = verification_loop._plan_next_round(row)
    assert next_round == 2
    assert cap == MAX_ROUNDS
    assert exhausted is False


# ---------------------------------------------------------------------------
# Test 5 — the extreme budget survives a block
# ---------------------------------------------------------------------------


def test_single_round_budget_survives_a_block(state_db, plans_dir):
    """``max_rounds = 1`` and the one round is blocked: the plan lives.

    The worst case for the fix: the plan has room for exactly one round
    and that round is the blocked one.  Charging the block to the
    budget would end the plan with ``max_rounds_reached`` — after a
    round that never ran, and with nothing verified.  A blocked round
    reuses its number, so the plan still has its one round to run.
    """
    _init_round(state_db, 1, 1)

    _record_block(1)
    row = _current(state_db)

    assert row["round"] == 1
    assert row["max_rounds"] == 1
    assert row["verification_stop_reason"] == STOP_REASON
    assert verification_loop._plan_next_round(row) == (1, 1, False)