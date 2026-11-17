"""Unit tests: the read model's waiting rows and the terminal summary.

The scheduler's read model answers three questions — *what is running*,
*what is waiting*, and *why is it waiting*.  Until now the third
question had one answer for everything: the queue's reason column said
「等待人工回答」 and stopped there.  The operator could not see the
question, could not see how much time was left, and could not tell one
waiting plan from another.

So the read model grew a **waiting list** and the terminal plan area
grew a **terminal summary**:

1. **The waiting row is a closed shape.**  Eight keys, always the same
   eight: the plan, the item, the kind of wait, the question, the
   deadline, the time left, whether the wait can still be extended and
   how many times it has been.  A row that carried a ninth key would be
   a field the panel cannot draw.
2. **The time left is an integer.**  It is counted against the tick's
   own clock, and a deadline that has passed answers zero — never a
   negative number, because the panel counts down.
3. **The summary comes from the report.**  The three fields of the
   ``postprocess`` summary are the report's own fields, read from
   ``postprocess_report.json`` — so the panel's terminal area and the
   report cannot tell the operator two different stories.
4. **The build stays a read.**  It starts no subprocess and opens no
   database: the snapshot is what everybody reads, and a read that
   shells out or opens a connection is a read that can block the tick.

The seven-key shape is asserted exactly — not as a subset.  The sixth
key became the seventh on purpose (the waiting list), and a subset
assertion would let the next key in without anybody deciding to add
it.
"""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from plan_scheduler.snapshot import (
    POSTPROCESS_KEYS,
    WAITING_KEYS,
    build,
    postprocess_summary,
    with_postprocess,
)
from postprocess.report import read_report, write_report


#: The tick's own moment — the clock the state carries.  Every number
#: the read model measures is measured against it, so two reads of the
#: same state say the same thing.
NOW = 1_700_000_000.0

#: The deadline of a wait that was created one week ago — the
#: architecture's default limit — so the time left on it is exactly
#: one week in seconds.
DEADLINE = datetime.fromtimestamp(
    NOW + 604800, tz=timezone.utc
).isoformat()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_record(
    item_id: str = "approve",
    *,
    wait_type: str = "human",
    question: str = "批准合入？",
    deadline: Optional[str] = DEADLINE,
    extensions: Optional[List[Dict[str, Any]]] = None,
    status: str = "pending",
) -> Dict[str, Any]:
    """A wait record, shaped like the one the wait registry hands out."""
    return {
        "item_id": item_id,
        "wait_type": wait_type,
        "question": question,
        "deadline": deadline,
        "extensions": extensions if extensions is not None else [],
        "status": status,
    }


def _waiting_row(plan_id: str, **overrides: Any) -> Dict[str, Any]:
    """A waiting row as the state carries it: a plan and its wait."""
    row = {"plan_id": plan_id}
    row.update(_wait_record())
    row.update(overrides)
    return row


def _state(**overrides: Any) -> Dict[str, Any]:
    """The state of one tick, as the engine hands it to the snapshot."""
    state = {
        "capacity": 4,
        "used": 1,
        "now": NOW,
        "min_runtime_seconds": 600,
        "queue": [],
        "running": [],
    }
    state.update(overrides)
    return state


def _write_report(plans_dir: Path, plan_id: str = "p1") -> Dict[str, Any]:
    """Write a terminal report for ``plan_id`` under ``plans_dir``."""
    return write_report(
        plan_id,
        frozen_spec={"spec_sha256": "c" * 64},
        item_verdicts=[
            {
                "id": "push",
                "verdict": "PASS",
                "evidence_excerpt": "exit=0",
                "audit": {
                    "action": "push",
                    "command": ["git", "push"],
                    "target": "origin",
                },
            },
            {
                "id": "tag",
                "verdict": "SKIPPED",
                "evidence_excerpt": "",
                "audit": {
                    "action": "tag",
                    "command": ["git", "tag"],
                    "target": "v1",
                },
            },
        ],
        plans_dir=plans_dir,
    )


# ---------------------------------------------------------------------------
# 1. The shape is closed — seven keys, and the waiting row's eight
# ---------------------------------------------------------------------------


def test_snapshot_has_eight_keys() -> None:
    """The read model carries exactly eight keys.

    The seventh is ``waiting`` — added on purpose, because the panel
    could not show what the operator was waiting for.  The eighth is
    ``parked`` — the plans stopped for want of provider capacity, which
    hold no slot and are in no line.  The set is compared exactly and
    not as a subset: a subset assertion would let the next key in
    without anybody deciding to add it.
    """
    result = build(_state())

    assert set(result) == {
        "capacity",
        "queue_length",
        "over_capacity",
        "queue",
        "running",
        "waiting",
        "parked",
        "generated_at",
    }


def test_waiting_rows_are_closed_shape() -> None:
    """A waiting row carries exactly the eight keys the panel draws.

    The row names the plan and the item, says what kind of wait it is
    and what it is asking, and answers the two questions the operator
    asks of a countdown: how long is left, and can it still be pushed
    out.
    """
    result = build(_state(waits=[_waiting_row("p1")]))

    assert len(result["waiting"]) == 1
    assert set(result["waiting"][0]) == {
        "plan_id",
        "item_id",
        "wait_type",
        "question",
        "deadline",
        "remaining_seconds",
        "extendable",
        "extensions_count",
    }
    # The eight keys are the module's own declaration, not a copy of
    # them written out here: a copy would keep passing after the row
    # grew a ninth key.
    assert set(result["waiting"][0]) == set(WAITING_KEYS)


def test_waiting_is_empty_list_when_no_waits() -> None:
    """Nothing waits → an empty list, never ``null``.

    ``null`` would be a second answer to "is anything waiting": the
    panel would have to decide what an absent list means, and the LUI
    would have to decide it a second time.
    """
    result = build(_state())

    assert result["waiting"] == []
    assert isinstance(result["waiting"], list)


def test_legacy_six_keys_still_present_with_same_types() -> None:
    """The six keys the read model carried before still carry the same types.

    Adding the waiting list changed the *number* of keys, not what the
    old ones mean: the capacity is still a block, the queue length
    still a number, the two lists still lists.
    """
    result = build(
        _state(
            capacity=4,
            used=5,
            queue=[
                {
                    "plan_id": "p1",
                    "seq": 1,
                    "priority": 0,
                    "promote_order": None,
                    "due": True,
                    "reason": "waiting_for_slot",
                }
            ],
            running=[
                {
                    "plan_id": "r1",
                    "acquired_at": NOW - 30.0,
                    "workspace": "/ws",
                }
            ],
        )
    )

    assert isinstance(result["capacity"], dict)
    assert isinstance(result["queue_length"], int)
    assert isinstance(result["over_capacity"], bool)
    assert isinstance(result["queue"], list)
    assert isinstance(result["running"], list)
    assert isinstance(result["generated_at"], str)
    # The old six are still there, with the old meanings.
    assert result["queue_length"] == 1
    assert result["over_capacity"] is True
    assert result["capacity"] == {"total": 4, "used": 5, "source": "config"}


# ---------------------------------------------------------------------------
# 2. The waiting row says what the operator needs
# ---------------------------------------------------------------------------


def test_waiting_row_carries_the_wait_it_was_given() -> None:
    """The row passes the wait's own facts on: the item, the kind, the question."""
    row = _waiting_row(
        "p1",
        item_id="approve",
        wait_type="external",
        question="CI 绿了吗？",
    )

    result = build(_state(waits=[row]))

    entry = result["waiting"][0]
    assert entry["plan_id"] == "p1"
    assert entry["item_id"] == "approve"
    assert entry["wait_type"] == "external"
    assert entry["question"] == "CI 绿了吗？"
    assert entry["deadline"] == DEADLINE


def test_remaining_seconds_counts_against_the_ticks_clock() -> None:
    """The time left is counted against the moment the state carries.

    It is not the wall clock: two reads of the same state have to say
    the same thing, or the countdown on the panel would jump between
    two reads that nobody asked for.
    """
    result = build(_state(now=NOW, waits=[_waiting_row("p1")]))

    assert result["waiting"][0]["remaining_seconds"] == 604800


def test_remaining_seconds_is_zero_when_the_deadline_passed() -> None:
    """A deadline that has passed leaves zero — never a negative number.

    The panel counts down; a countdown that goes below zero would be
    showing a time that does not exist.
    """
    past = datetime.fromtimestamp(NOW - 1, tz=timezone.utc).isoformat()

    result = build(_state(now=NOW, waits=[_waiting_row("p1", deadline=past)]))

    assert result["waiting"][0]["remaining_seconds"] == 0


def test_remaining_seconds_is_a_whole_number_of_seconds() -> None:
    """The time left is an integer, not a fraction of a second."""
    result = build(_state(now=NOW, waits=[_waiting_row("p1")]))

    remaining = result["waiting"][0]["remaining_seconds"]
    assert isinstance(remaining, int)
    assert not isinstance(remaining, bool)


def test_extensions_count_counts_the_pushes() -> None:
    """The row says how many times the deadline has been pushed out."""
    extensions = [
        {"operator": "ops", "when": "2026-11-14T00:00:00+00:00",
         "from": "a", "to": "b"},
        {"operator": "ops", "when": "2026-11-14T01:00:00+00:00",
         "from": "b", "to": "c"},
    ]

    result = build(
        _state(waits=[_waiting_row("p1", extensions=extensions)])
    )

    assert result["waiting"][0]["extensions_count"] == 2


def test_a_pending_wait_is_extendable_and_a_settled_one_is_not() -> None:
    """A wait can be pushed out exactly while it is still pending.

    A settled wait — answered, terminated or failed — is not: its
    deadline is a deadline nobody is watching any more.
    """
    pending = build(_state(waits=[_waiting_row("p1", status="pending")]))
    answered = build(_state(waits=[_waiting_row("p1", status="answered")]))

    assert pending["waiting"][0]["extendable"] is True
    assert answered["waiting"][0]["extendable"] is False


def test_waiting_rows_keep_the_order_the_state_gave_them() -> None:
    """Two waiting plans come out in the order the state listed them."""
    state = _state(
        waits=[_waiting_row("p1", item_id="approve"), _waiting_row("p2", item_id="ci")]
    )

    result = build(state)

    assert [row["plan_id"] for row in result["waiting"]] == ["p1", "p2"]
    assert [row["item_id"] for row in result["waiting"]] == ["approve", "ci"]


def test_a_wait_without_an_item_is_not_a_wait() -> None:
    """The engine's own ``waiting`` key means "plans waiting for a slot".

    Those rows are queued plans, not waits: a read model that showed
    them as waiting rows would put plans the scheduler is merely
    queuing into the panel's waiting block.
    """
    state = _state(
        waiting=[
            {"plan_id": "p1", "priority": 0, "seq": 1, "due": True},
        ]
    )

    result = build(state)

    assert result["waiting"] == []


# ---------------------------------------------------------------------------
# 3. The terminal summary comes from the report
# ---------------------------------------------------------------------------


def test_terminal_plan_carries_postprocess_summary(tmp_path: Path) -> None:
    """A terminal plan carries the summary, field for field, from the report.

    The report is the terminal truth — the panel's terminal area, the
    failure notification and the resume entry point all read it.  The
    summary is that same truth with three of its fields, so the panel
    and the report cannot tell the operator two different stories.
    """
    _write_report(tmp_path, "p1")

    entry = with_postprocess({"plan_id": "p1"}, plans_dir=tmp_path)
    report = read_report("p1", tmp_path)

    assert set(entry["postprocess"]) == set(POSTPROCESS_KEYS)
    assert set(entry["postprocess"]) == {
        "spec_sha256",
        "terminal_status",
        "item_verdicts",
    }
    assert entry["postprocess"]["spec_sha256"] == report["spec_sha256"]
    assert entry["postprocess"]["terminal_status"] == report["terminal_status"]
    assert entry["postprocess"]["item_verdicts"] == report["items"]
    # The verdicts keep the report's order: a panel that showed them
    # in another order would be showing another list.
    assert [
        item["id"] for item in entry["postprocess"]["item_verdicts"]
    ] == [item["id"] for item in report["items"]]


def test_the_summary_stands_alone_without_the_entry(tmp_path: Path) -> None:
    """The summary is readable on its own — it is the report's three fields."""
    _write_report(tmp_path, "p1")

    summary = postprocess_summary("p1", plans_dir=tmp_path)
    report = read_report("p1", tmp_path)

    assert summary is not None
    assert summary["spec_sha256"] == report["spec_sha256"]
    assert summary["terminal_status"] == report["terminal_status"]
    assert summary["item_verdicts"] == report["items"]


def test_a_plan_without_a_report_has_no_summary(tmp_path: Path) -> None:
    """A plan that never reached the finishing stage has no terminal result."""
    summary = postprocess_summary("p2", plans_dir=tmp_path)

    assert summary is None

    entry = with_postprocess({"plan_id": "p2"}, plans_dir=tmp_path)
    assert "postprocess" not in entry
    # The entry the caller had is handed back as it was.
    assert entry == {"plan_id": "p2"}


# ---------------------------------------------------------------------------
# 4. The build stays a read
# ---------------------------------------------------------------------------


def test_the_build_starts_no_subprocess_and_opens_no_database(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Nothing in the read model shells out or opens a connection.

    The snapshot is what everybody reads — the panel polls it every few
    seconds — so a read that spawns a process or opens a database is a
    read that can block the tick it is supposed to describe.
    """

    def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the read model must not do this")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "check_output", _forbidden)
    monkeypatch.setattr(sqlite3, "connect", _forbidden)

    _write_report(tmp_path, "p1")

    result = build(_state(waits=[_waiting_row("p1")]))
    summary = postprocess_summary("p1", plans_dir=tmp_path)

    assert result["waiting"][0]["plan_id"] == "p1"
    assert summary is not None
    assert summary["terminal_status"] == "completed"