"""The failure history: one record per round, and no erased evidence.

2026-10-29 (VP-030, root-cause report ``root-cause-VP-030.md`` §4.9, R7).

The plan ran rounds 1, 2 and 3, then the round counter went back to 1 —
and the round-1 write **overwrote** round 1's record.  The history held
three records for four failed rounds, and the earliest round's original
evidence was gone.  "连续第几次失败" and "上一轮到底说了什么" both became
unanswerable, and those are exactly the two questions the next round's
diagnosis is made of.

Two properties have to hold now:

* a round that has already run is not run again;
* a record that is written over is not **erased** — the earlier verdict
  stays with the entry, so the evidence chain survives.

Adapted to this branch (origin/main).  The **refusing** half of the
upstream change is not here, and the reason is structural rather than
an omission: upstream the round number was carried by the plan's own
state file and could be reset, so a guard had to refuse a round below
the history's high-water mark.  On this branch the round number is
derived from the plan's row in ``state.db`` — ``_plan_next_round`` in
``backend/verification_loop.py`` answers ``current_round + 1``, and a
round that was blocked before it dispatched anything is re-run under the
number it was blocked at.  There is no counter that can walk back, so
there is nothing for a guard to refuse.  What is left is the half that
belongs to the history itself: the high-water mark it publishes, and the
evidence chain that survives a record being written over.

Every assertion below drives production code.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification import failure_history as fh  # noqa: E402


VP_ID = "VP-030"
PLAN_ID = "20260101-example-plan"


def _plan_dir(tmp_path: Path) -> Path:
    plan_dir = tmp_path / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


def _record(round_number: int, actual_result: str, evidence: str = "log") -> Dict[str, Any]:
    return {
        "id": VP_ID,
        "round": round_number,
        "actual_result": actual_result,
        "evidence": evidence,
        "failure_class": "deliverable_defect",
        "failure_class_source": "stated",
    }


# ---------------------------------------------------------------------------
# The high-water mark: the rounds that have already run
# ---------------------------------------------------------------------------


def test_the_high_water_mark_is_the_highest_round_recorded(tmp_path):
    """A round below it has already run — writing it again would erase."""
    plan_dir = _plan_dir(tmp_path)
    fh.save(plan_dir, {VP_ID: [_record(3, "第三轮")]})

    history = fh.load(plan_dir)

    assert fh.highest_recorded_round(history) == 3


def test_a_plan_that_has_never_run_has_no_high_water_mark(tmp_path):
    """``0`` means "no round has been recorded yet"."""
    plan_dir = _plan_dir(tmp_path)

    assert fh.highest_recorded_round(fh.load(plan_dir)) == 0


def test_the_high_water_mark_is_read_from_the_saved_history(tmp_path):
    """The mark is the plan's own record, not the caller's memory."""
    plan_dir = _plan_dir(tmp_path)
    fh.save(plan_dir, {VP_ID: [_record(1, "第一轮"), _record(2, "第二轮")]})

    assert fh.highest_recorded_round(fh.load(plan_dir)) == 2


# ---------------------------------------------------------------------------
# The evidence chain survives
# ---------------------------------------------------------------------------


def test_a_record_written_over_keeps_the_earlier_verdict():
    """The harm of R7: the earliest round's original evidence was gone."""
    history: Dict[str, List[Dict[str, Any]]] = {}
    fh.merge_round(history, {VP_ID: {"actual_result": "第一批的理由", "evidence": "old"}}, 1)
    fh.merge_round(history, {VP_ID: {"actual_result": "这一批的理由", "evidence": "new"}}, 1)

    assert len(history[VP_ID]) == 1, "一轮一条 —— 重复条目会让计数与历史对不上"
    entry = history[VP_ID][0]
    # The freshest verdict is the one that counts (2026-09-19).
    assert entry["actual_result"] == "这一批的理由"
    assert entry["evidence"] == "new"
    # …and the earlier one is not erased.
    assert entry["superseded_verdict"]["actual_result"] == "第一批的理由"
    assert entry["superseded_verdict"]["evidence"] == "old"


def test_the_same_round_written_with_the_same_content_stays_one_record():
    history: Dict[str, List[Dict[str, Any]]] = {}
    payload = {VP_ID: {"actual_result": "同一批的理由", "evidence": "same"}}
    fh.merge_round(history, payload, 2)
    fh.merge_round(history, payload, 2)

    assert len(history[VP_ID]) == 1
    assert "superseded_verdict" not in history[VP_ID][0]


def test_one_failure_leaves_one_record_per_round():
    """The invariant A8 states: the history and the count agree."""
    history: Dict[str, List[Dict[str, Any]]] = {}
    for round_number in (1, 2, 3, 4):
        fh.merge_round(
            history,
            {VP_ID: {"actual_result": f"第 {round_number} 轮", "evidence": ""}},
            round_number,
        )

    assert [entry["round"] for entry in history[VP_ID]] == [1, 2, 3, 4]
    assert len(history[VP_ID]) == 4
