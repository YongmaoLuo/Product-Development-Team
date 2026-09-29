"""F2 (2026-09-17): superseded DB-only rows must not reach the card.

Earlier production observation (2026-09-17):

  * the failure list showed three content-free ``Task X (DB-only)`` rows —
    ``repair-r3-03-1`` / ``repair-r3-03-1-1`` / ``repair-r3-06``, which are
    refiner *split parents* that the refiner had already replaced;
  * "最近完成活动" named those same parents, because the plan had been idle
    since the split so their ``end_ts`` were the five most recent;
  * the progress bar read ``79% (44/56)`` — the denominator counted ten
    ledger rows the dispatcher had already superseded at startup
    (``task_orphans_reconciled: superseded 10``).

The rows exist only in ``plan_tasks``; ``tasks.json`` — the plan's DAG —
has no such task, and the dispatcher refuses to schedule them. They are
carried into the API payloads with ``_origin="db_orphan_terminal"``.

Contract pinned here:

  * ``drop_terminal_db_orphans`` removes exactly those rows from the task
    list and decrements their statuses from the counts (clamped at 0),
    without mutating its inputs (the notifier persists raw snapshots);
  * a card built from an orphan-laden payload contains no ``(DB-only)``
    placeholder and no split-parent id anywhere in its text;
  * the progress bar counts the plan, not the ledger (46, not 56);
  * non-terminal orphans (rows the dispatcher WILL schedule) survive.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from notifications.cards import (  # noqa: E402
    build_card,
    drop_terminal_db_orphans,
)
from status_payload import status_from  # noqa: E402


def _task(tid: str, status: str, **extra: Any) -> Dict[str, Any]:
    out = {
        "id": tid,
        "title": f"task {tid}",
        "status": status,
        "end_ts": "2026-09-17T00:41:15Z" if status != "pending" else None,
    }
    out.update(extra)
    return out


def _summary(tasks_info: Dict[str, int], **extra: Any) -> Dict[str, Any]:
    out = {
        "plan_id": "plan-orphans",
        "state": {"current_phase": "executing", "stage": "executing"},
        "tasks": dict(tasks_info),
        "docs": {},
    }
    out.update(extra)
    return out


def _all_text(node: Any) -> str:
    """Flatten every string in a card dict into one blob for assertions."""
    parts: List[str] = []
    if isinstance(node, dict):
        for v in node.values():
            parts.append(_all_text(v))
    elif isinstance(node, list):
        for x in node:
            parts.append(_all_text(x))
    elif isinstance(node, str):
        parts.append(node)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


def test_helper_drops_marked_rows_and_recounts():
    summary = _summary({"total": 5, "completed": 2, "failed": 2, "pending": 1})
    progress = {
        "tasks": [
            _task("live-1", "completed"),
            _task("parent-a", "failed", _origin="db_orphan_terminal",
                  failure_reason=None),
            _task("parent-b", "failed", _origin="db_orphan_terminal"),
            _task("live-2", "pending"),
        ],
        "counts": {"total": 4, "completed": 2, "failed": 2, "pending": 1},
        "current": None,
    }

    new_summary, new_progress = drop_terminal_db_orphans(summary, progress)

    assert [t["id"] for t in new_progress["tasks"]] == ["live-1", "live-2"]
    assert new_summary["tasks"] == {
        "total": 3, "completed": 2, "failed": 0, "pending": 1,
    }
    assert new_progress["counts"]["total"] == 2
    assert new_progress["counts"]["failed"] == 0

    # Inputs untouched — the notifier persists the raw payloads.
    assert len(progress["tasks"]) == 4
    assert summary["tasks"]["total"] == 5


def test_helper_is_a_noop_without_orphans():
    summary = _summary({"total": 1, "completed": 1})
    progress = {"tasks": [_task("live", "completed")]}
    new_summary, new_progress = drop_terminal_db_orphans(summary, progress)
    assert new_summary is summary
    assert new_progress is progress


def test_helper_survives_none_and_junk_payloads():
    for summary, progress in (
        (None, None), ({}, {}), ({"tasks": "x"}, {"tasks": "y"}), ({}, None),
    ):
        assert drop_terminal_db_orphans(summary, progress) == (summary, progress)


def test_counts_never_go_negative():
    summary = _summary({"total": 1, "failed": 1})
    progress = {
        "tasks": [
            _task("p1", "failed", _origin="db_orphan_terminal"),
            _task("p2", "failed", _origin="db_orphan_terminal"),
            _task("p3", "failed", _origin="db_orphan_terminal"),
        ],
    }
    new_summary, _ = drop_terminal_db_orphans(summary, progress)
    assert new_summary["tasks"]["failed"] == 0
    assert new_summary["tasks"]["total"] == 0


# ---------------------------------------------------------------------------
# End to end through the card builder — the operator-visible contract
# ---------------------------------------------------------------------------


def _orphan_laden_card() -> Dict[str, Any]:
    """Card inputs shaped like an earlier plan.

    46 DAG tasks (44 completed, 1 in progress, 1 pending) plus the ten
    superseded ledger rows the plan actually carried — three of them
    refiner split parents with no content at all.
    """
    # The ten ledger rows: 7 failed + 3 completed, all outside the DAG.
    orphans = [
        _task("repair-r3-03-1", "failed", title="", failure_reason=None,
              _origin="db_orphan_terminal"),
        _task("repair-r3-03-1-1", "failed", title="", failure_reason=None,
              _origin="db_orphan_terminal"),
        _task("repair-r3-06", "failed", title="", failure_reason=None,
              _origin="db_orphan_terminal"),
        _task("11-5-1", "failed", title="", _origin="db_orphan_terminal"),
        _task("11-5-1-1", "failed", title="", _origin="db_orphan_terminal"),
        _task("repair-r4-01", "failed", _origin="db_orphan_terminal"),
        _task("repair-r4-03", "failed", _origin="db_orphan_terminal"),
        _task("repair-r4-02", "completed", _origin="db_orphan_terminal"),
        _task("repair-r4-04", "completed", _origin="db_orphan_terminal"),
        _task("repair-r3-03", "completed", _origin="db_orphan_terminal"),
    ]
    # 46 DAG tasks, of which two are the live ones the card must name.
    live = [_task("repair-r3-06-3", "in_progress"),
            _task("repair-r3-06-4", "pending")]
    dag_done = [_task(f"t-{i}", "completed") for i in range(44)]

    # Ledger view = DAG + orphans.
    summary = _summary({"total": 56, "completed": 47, "failed": 7,
                        "in_progress": 1, "pending": 1})
    progress = {
        "tasks": dag_done + live + orphans,
        "counts": {"total": 56, "completed": 47, "failed": 7,
                   "in_progress": 1, "pending": 1},
        "current": {"id": "repair-r3-06-3"},
        "execution_status": "running",
    }
    return build_card(
        "plan-orphans",
        status_from(summary, execution=progress, plan_id="plan-orphans"),
        summary, execution_progress=progress,
    )


def test_card_text_never_mentions_a_superseded_row():
    import re

    text = _all_text(_orphan_laden_card())
    assert "(DB-only)" not in text, "content-free ledger rows must not render"
    for tid in ("repair-r3-03-1", "repair-r3-03-1-1", "repair-r3-06",
                "11-5-1", "11-5-1-1", "repair-r4-01", "repair-r4-03"):
        # Match the rendered ``[id]`` token, not a substring: the live task
        # ``repair-r3-06-3`` legitimately contains ``repair-r3-06``.
        assert not re.search(rf"\[{re.escape(tid)}\]", text), (
            f"superseded row {tid} leaked into the card"
        )


def test_card_counts_the_plan_not_the_ledger():
    text = _all_text(_orphan_laden_card())
    assert "(44/46)" in text, (
        "the progress bar must count the plan's 46 tasks, not the 56 ledger rows"
    )
    assert "/56)" not in text


def test_card_still_shows_live_tasks():
    """The filter must not swallow real work."""
    text = _all_text(_orphan_laden_card())
    assert "repair-r3-06-3" in text
    assert "repair-r3-06-4" in text


def test_card_keeps_non_terminal_orphans():
    """A DB-only row the dispatcher WILL pick up is still real work."""
    summary = _summary({"total": 2, "completed": 1, "pending": 1})
    progress = {
        "tasks": [
            _task("live", "completed"),
            _task("recovered", "pending", _origin="db_orphan"),
        ],
        "counts": {"total": 2, "completed": 1, "pending": 1},
        "current": None,
    }
    text = _all_text(build_card(
        "plan-orphans",
        status_from(summary, execution=progress, plan_id="plan-orphans"),
        summary, execution_progress=progress,
    ))
    assert "recovered" in text


def test_card_inputs_are_not_mutated():
    """The notifier stores the raw payloads alongside the card."""
    summary = _summary({"total": 56, "completed": 45, "failed": 7,
                        "in_progress": 1, "pending": 3})
    progress = {
        "tasks": [_task("p", "failed", _origin="db_orphan_terminal")],
        "counts": {"total": 56, "failed": 7},
        "current": None,
    }
    before = json.dumps([summary, progress], sort_keys=True)
    build_card(
        "plan-orphans",
        status_from(summary, execution=progress, plan_id="plan-orphans"),
        summary, execution_progress=progress,
    )
    assert json.dumps([summary, progress], sort_keys=True) == before
