"""The "❌ 失败任务详情" section: which failures the card shows.

History, in two acts.

**2026-09-11 (v9) / 2026-09-14 (v14)** — failed tasks were missing from the
card: the endpoint hydrate had skipped terminal DB-only orphans, so the
failure section came back empty even though tasks had failed. v9 made them
flow through as
``db_orphan_terminal`` placeholders carrying ``failure_reason`` / ``end_ts``
(title resolved from the row when it has one, else ``Task {id} (DB-only)``).

**2026-09-17** — superseded tasks must not appear on the card at all.
And that is what those placeholders are: rows that exist only in the
``plan_tasks`` ledger, outside the plan's DAG. The dispatcher refuses to
schedule them (``agent._load_tasks`` Phase 2 supersedes terminal orphans;
the executor logs ``task_orphans_reconciled`` at startup), so rendering
them under ❌ claims a failure that is not part of the plan's work. On an
earlier plan, three refiner split *parents* surfaced this way as
content-free "Task X (DB-only)" rows, and — because the plan had been idle
since the split — they were also the most recent "完成活动" entries.

The new contract, pinned here:

  * a failed task **in the DAG** still renders its id, title and reason
    (truncated, with the ``未知原因`` fallback);
  * a terminal DB-only orphan is **not rendered at all** and does not
    count toward the bar;
  * a **non-terminal** DB-only orphan — work the dispatcher will actually
    pick up — still renders;
  * the filtering lives in ``cards.drop_terminal_db_orphans``; if the
    operator ever wants the ledger rows back, that is the one place.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _card_text(card: dict) -> str:
    """Concatenate every ``text.content`` line in the card body."""
    parts: list = []
    for el in card.get("elements", []):
        if el.get("tag") == "div":
            content = el.get("text", {}).get("content", "")
            if content:
                parts.append(content)
    return "\n".join(parts)


def _summary(failed: int = 0, total: int = 5) -> dict:
    """Build a minimal ``summary`` payload matching ``/api/plan/{id}/summary``."""
    return {
        "state": {
            "current_phase": "completed",
            "stage": "completed",
            "verification": {"status": "passed", "round": 1, "max_rounds": 3},
        },
        "tasks": {
            "total": total,
            "completed": total - failed,
            "failed": failed,
            "skipped": 0,
            "in_progress": 0,
            "pending": 0,
        },
    }


def _progress(tasks: list) -> dict:
    """Build a ``progress`` payload matching ``/api/execution/{id}/progress``."""
    counts = {
        "total": len(tasks),
        "completed": sum(1 for t in tasks if t.get("status") == "completed"),
        "failed": sum(1 for t in tasks if t.get("status") == "failed"),
        "in_progress": sum(1 for t in tasks if t.get("status") == "in_progress"),
        "pending": sum(1 for t in tasks if t.get("status") == "pending"),
    }
    return {
        "plan_id": "test-plan",
        "execution_status": "completed",
        "tasks": tasks,
        "counts": counts,
        "current": None,
        "next": None,
    }


def _failed_orphan(task_id: str, reason: str = "boom", **extra) -> dict:
    """A ``db_orphan_terminal`` placeholder: ledger-only, no DAG entry."""
    out = {
        "id": task_id,
        "status": "failed",
        "failure_reason": reason,
        "end_ts": "2026-09-06T10:00:00",
        "_origin": "db_orphan_terminal",
    }
    out.update(extra)
    return out


def _disk_failed(task_id: str, title: str, reason: str) -> dict:
    """A failed task that IS in the plan's DAG (tasks.json)."""
    return {
        "id": task_id,
        "title": title,
        "status": "failed",
        "failure_reason": reason,
        "end_ts": "2026-09-06T10:00:00",
    }


# --- The DAG's own failures still render -----------------------------------


def test_disk_failed_task_renders_id_title_and_reason():
    from notifications.cards import build_progress_card

    progress = _progress([
        _disk_failed(
            "11-2",
            "Rust:把 trend/consolidation 分类接入生产 emit 路径",
            "metric field not present in ItemInfo",
        ),
    ])
    summary = _summary(failed=1, total=10)

    text = _card_text(build_progress_card("test-plan", summary, progress=progress))

    assert "❌ 失败任务详情" in text
    assert "[11-2]" in text
    assert "trend/consolidation" in text
    assert "metric field" in text


def test_disk_failed_task_long_reason_truncated_to_200_chars():
    from notifications.cards import build_progress_card

    long_reason = "x" * 500
    progress = _progress([_disk_failed("LONG", "t", long_reason)])
    summary = _summary(failed=1, total=1)

    text = _card_text(build_progress_card("test-plan", summary, progress=progress))
    assert "..." in text, "Long failure_reason should be truncated with ..."
    assert long_reason not in text, "Full 500-char reason should NOT appear"


def test_disk_failed_task_missing_reason_shows_unknown_reason():
    from notifications.cards import build_progress_card

    task = _disk_failed("X", "title", "")
    task["failure_reason"] = None
    progress = _progress([task])
    summary = _summary(failed=1, total=1)

    text = _card_text(build_progress_card("test-plan", summary, progress=progress))
    assert "[X]" in text
    assert "未知原因" in text


# --- Superseded ledger rows are gone (2026-09-17) --------------------------


def test_terminal_db_orphan_is_not_rendered():
    """The v9/v14 behavior is deliberately reversed — see the module docstring."""
    from notifications.cards import build_progress_card

    progress = _progress([
        _failed_orphan("40-1", "upstream_failed:39-2 — task body parser crashed"),
        _failed_orphan("R1-5", "manual_unstick_2026-09-06_pipe_deadlock"),
    ])
    summary = _summary(failed=2, total=2)

    text = _card_text(build_progress_card("test-plan", summary, progress=progress))

    assert "[40-1]" not in text
    assert "[R1-5]" not in text
    assert "(DB-only)" not in text
    assert "❌ 失败任务详情" not in text, (
        "a card whose only failures are superseded rows must not claim failures"
    )


def test_terminal_db_orphan_with_real_title_is_still_not_rendered():
    """Having a resolved title does not make a superseded row live work."""
    from notifications.cards import build_progress_card

    progress = _progress([
        _failed_orphan("R1-5", "test_command exited 1",
                       title="修复 VP-013 的 test_command"),
    ])
    summary = _summary(failed=1, total=1)

    text = _card_text(build_progress_card("test-plan", summary, progress=progress))
    assert "修复 VP-013 的 test_command" not in text
    assert "[R1-5]" not in text


def test_non_terminal_db_orphan_still_renders():
    """A ledger row the dispatcher WILL schedule is real work — keep it."""
    from notifications.cards import build_progress_card

    recovered = {
        "id": "recovered-1",
        "title": "recovered task",
        "status": "pending",
        "_origin": "db_orphan",
    }
    progress = _progress([recovered])
    summary = _summary(failed=0, total=1)

    text = _card_text(build_progress_card("test-plan", summary, progress=progress))
    assert "recovered-1" in text


# --- Counts follow the same view -------------------------------------------


def test_terminal_orphans_do_not_count_toward_the_bar():
    """The endpoint counts include ledger rows; the card must not.

    Mirrors server.py's count derivation from ``tasks[]`` — which the v9
    note pinned as the counterpart invariant — but the card now
    normalises first, so the two agree on the *plan's* task set.
    """
    from notifications.cards import build_progress_card

    progress = _progress([
        _disk_failed("live-fail", "t", "r"),
        _failed_orphan("a", "r1"),
        _failed_orphan("b", "r2"),
    ])
    summary = _summary(failed=3, total=3)

    text = _card_text(build_progress_card("test-plan", summary, progress=progress))

    assert "(0/1)" in text, f"only the DAG task should be counted. Got:\n{text}"
    assert "❌1" in text, f"the bar's failed badge must be 1, not 3. Got:\n{text}"


@pytest.mark.parametrize("status", ["completed", "failed", "superseded"])
def test_every_terminal_orphan_status_is_filtered(status):
    from notifications.cards import build_progress_card

    orphan = _failed_orphan("gone", "r", status=status)
    progress = _progress([orphan])
    summary = _summary(failed=1, total=1)

    text = _card_text(build_progress_card("test-plan", summary, progress=progress))
    assert "[gone]" not in text
