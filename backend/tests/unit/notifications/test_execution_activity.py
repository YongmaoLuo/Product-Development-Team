"""The executor's non-task work is a first-class "current activity".

2026-10-05. Measured on a real 30-hour plan (``20261004-PDT-Product-
Developm``): the refiner ran 34 times, median 4 minutes, 140 minutes
total. Every one of those windows has the same shape —

  * no ``plan_tasks`` row is ``in_progress``, because the refiner
    rewrites the task list and writes no row of its own;
  * ``execution_logger._TASK_LIFECYCLE_EVENTS`` deliberately
    whitelists only the five ``task_*`` events, so no bus event fires;
  * ``_watch_execution_progress`` fingerprinted task rows only, so its
    synthetic refresh did not fire either.

The card therefore showed "正在跑 tasks" and named nothing. 17 of 85
executing pushes were nameless; 14 of those 17 landed in or beside a
refine window.

What is pinned here:

  * ``current_activity`` reports the refine window from the log tail,
    with the triggering task id;
  * it reports ``idle`` (not the last thing it saw) once the handoff
    event lands, so a stalled executor is distinguishable from one
    between units of work;
  * an unreadable / absent log degrades to ``unknown`` rather than
    raising — the card falls back to the bare phase label;
  * the watch fingerprint moves when the activity changes, which is
    what re-arms the card refresh;
  * the unified line names the activity when no task is running, and
    the task row still wins when both are available.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from execution_logger import ExecutionLogger, _read_tail
from notifications.cards import _activity_line, _unified_phase_section
from notifications.feishu_notifier import FeishuNotifier


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def plans_dir(tmp_path: Path) -> Path:
    (tmp_path / "p1").mkdir(parents=True)
    return tmp_path


def _write_log(plans_dir: Path, entries: list) -> None:
    lines = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
    (plans_dir / "p1" / "execution.log").write_text(lines, encoding="utf-8")


def _ev(event: str, ts: str, **data) -> dict:
    return {"ts": ts, "level": "INFO", "event": event, "message": event, "data": data}


# ---------------------------------------------------------------------------
# current_activity derivation
# ---------------------------------------------------------------------------


def test_reports_refine_window_with_triggering_task(plans_dir: Path):
    """A refine in progress is named, and so is the failure that caused it."""
    _write_log(plans_dir, [
        _ev("task_started", "2026-10-05T06:10:00", title="do the thing"),
        _ev("task_failed", "2026-10-05T06:15:00"),
        _ev("refine_started", "2026-10-05T06:16:00", title="do the thing"),
        # Interleaved noise between the start and the observation — the
        # derivation must skip it rather than treat it as the answer.
        _ev("file_lock_acquired", "2026-10-05T06:17:00"),
        _ev("provider_selected", "2026-10-05T06:19:00"),
    ])

    activity = ExecutionLogger.current_activity("p1", plans_dir)

    assert activity["kind"] == "refine"
    assert activity["started_at"] == "2026-10-05T06:16:00"
    assert activity["event"] == "refine_started"


def test_refine_line_names_the_failed_task(plans_dir: Path):
    _write_log(plans_dir, [
        _ev("refine_started", "2026-10-05T06:16:00", title="t"),
    ])
    # task_id on the event is what the card renders as the cause.
    entry = _ev("refine_started", "2026-10-05T06:16:00", title="t")
    entry["task_id"] = "20-5-1-3"
    _write_log(plans_dir, [entry])

    line = _activity_line(ExecutionLogger.current_activity("p1", plans_dir))

    assert "正在精化任务列表" in line
    assert "`20-5-1-3`" in line


def test_handoff_reports_idle_not_the_last_task(plans_dir: Path):
    """Once the refine ends, the executor has nothing in flight.

    Reporting the refine as still-running here is how a card ends up
    claiming work that finished minutes ago.
    """
    _write_log(plans_dir, [
        _ev("refine_started", "2026-10-05T06:16:00", title="t"),
        _ev("refine_structure_applied", "2026-10-05T06:20:00"),
    ])

    activity = ExecutionLogger.current_activity("p1", plans_dir)

    assert activity["kind"] == "idle"
    assert activity["event"] == "refine_structure_applied"


def test_layer_boundary_is_its_own_kind(plans_dir: Path):
    """Distinct from refine: a boundary is seconds, a refine is minutes."""
    _write_log(plans_dir, [
        _ev("layer_completed", "2026-10-05T12:20:02"),
        _ev("layer_started", "2026-10-05T12:20:03"),
    ])

    assert ExecutionLogger.current_activity("p1", plans_dir)["kind"] == "layer_boundary"


def test_running_task_is_reported_as_task_kind(plans_dir: Path):
    _write_log(plans_dir, [
        _ev("task_started", "2026-10-05T14:39:20", title="repair"),
    ])

    activity = ExecutionLogger.current_activity("p1", plans_dir)

    assert activity["kind"] == "task"
    # ``task`` contributes nothing to the card — the task sections
    # already name it — so _activity_line must stay silent for it.
    assert _activity_line(activity) == ""


def test_missing_log_degrades_to_unknown(plans_dir: Path):
    activity = ExecutionLogger.current_activity("p1", plans_dir)

    assert activity["kind"] == "unknown"
    assert _activity_line(activity) == ""


def test_unrecognised_events_only_degrades_to_unknown(plans_dir: Path):
    _write_log(plans_dir, [_ev("some_future_event", "2026-10-05T00:00:00")])

    assert ExecutionLogger.current_activity("p1", plans_dir)["kind"] == "unknown"


# ---------------------------------------------------------------------------
# Tail reading
# ---------------------------------------------------------------------------
#
# ``current_activity`` seeks backwards from EOF rather than parsing the
# whole file, because it runs on the endpoints the notifier polls
# continuously and a long run's log is megabytes and growing. These pin
# the cases where a seek differs from a full read.


def test_tail_read_ignores_a_partial_first_line(plans_dir: Path, monkeypatch):
    """Seeking lands mid-line. The fragment cannot be parsed and must be
    dropped, not allowed to abort the read."""
    _write_log(plans_dir, [
        _ev("refine_started", "2026-10-05T06:16:00", title="t"),
        _ev("file_lock_acquired", "2026-10-05T06:17:00"),
    ])
    # Shrink the starting budget so the seek path is the one exercised.
    monkeypatch.setattr("execution_logger._TAIL_INITIAL_BYTES", 8)

    activity = ExecutionLogger.current_activity("p1", plans_dir)

    assert activity["kind"] == "refine", activity


def test_tail_read_ignores_malformed_lines(plans_dir: Path):
    _write_log(plans_dir, [
        _ev("refine_started", "2026-10-05T06:16:00", title="t"),
    ])
    with open(plans_dir / "p1" / "execution.log", "a", encoding="utf-8") as f:
        f.write("{not json at all\n")
        f.write("[1,2,3]\n")  # valid JSON, wrong shape
        f.write("\n")

    assert ExecutionLogger.current_activity("p1", plans_dir)["kind"] == "refine"


def test_tail_read_works_on_a_file_larger_than_the_byte_cap(tmp_path: Path):
    """The realistic case: a long run's log far exceeds the cap, and the
    answer must still come from the end."""
    plans = tmp_path / "p1"
    plans.mkdir(parents=True)
    with open(plans / "execution.log", "w", encoding="utf-8") as f:
        for i in range(20000):
            f.write(json.dumps(_ev("file_lock_acquired", f"2026-10-05T00:00:{i%60:02d}")) + "\n")
        f.write(json.dumps(_ev("task_started", "2026-10-05T09:00:00", title="t")) + "\n")

    activity = ExecutionLogger.current_activity("p1", tmp_path)

    assert activity["kind"] == "task"
    assert activity["started_at"] == "2026-10-05T09:00:00"


def test_tail_read_matches_a_full_read(plans_dir: Path):
    """Same answer whichever path is taken — the optimisation must not
    be able to change what is reported."""
    entries = [
        _ev("task_started", "2026-10-05T05:00:00", title="t"),
        _ev("task_completed", "2026-10-05T05:10:00"),
        _ev("refine_started", "2026-10-05T05:11:00", title="t"),
    ]
    _write_log(plans_dir, entries)

    via_tail = ExecutionLogger.current_activity("p1", plans_dir)
    via_full = _read_tail(plans_dir / "p1" / "execution.log", limit=500)

    assert via_tail["kind"] == "refine"
    assert [e.get("event") for e in via_full][-1] == "refine_started"


def test_tail_read_handles_a_missing_file(tmp_path: Path):
    assert ExecutionLogger.current_activity("nope", tmp_path)["kind"] == "unknown"


def test_tail_read_handles_an_empty_file(plans_dir: Path):
    (plans_dir / "p1" / "execution.log").write_text("", encoding="utf-8")

    assert ExecutionLogger.current_activity("p1", plans_dir)["kind"] == "unknown"


# ---------------------------------------------------------------------------
# Card rendering
# ---------------------------------------------------------------------------


def _unified(**kwargs) -> str:
    out = _unified_phase_section("executing", verification_status="", **kwargs)
    return out[0]["text"]["content"] if out else ""


def test_unified_line_names_the_refine_when_no_task_is_running():
    line = _unified(
        current_task=None,
        current_activity={"kind": "refine", "started_at": None, "task_id": "9-9"},
    )

    assert "正在精化任务列表" in line
    assert "`9-9`" in line


def test_unified_line_prefers_the_task_row_over_the_activity():
    """A ``plan_tasks`` row is authoritative — it names what the executor
    committed to. The activity is the fallback, not a competitor."""
    line = _unified(
        current_task=("repair-r1-01", "把钥匙串改指独立钥匙串"),
        current_activity={"kind": "refine", "started_at": None},
    )

    assert "`[repair-r1-01]`" in line
    assert "正在精化" not in line


def test_unified_line_falls_back_to_the_old_label_with_no_activity():
    """Degraded paths (no log, unknown kind) keep the pre-existing text
    rather than rendering a blank or an invented state."""
    line = _unified(current_task=None, current_activity=None)

    assert "正在跑 tasks" in line


def test_idle_activity_is_distinguishable_from_silence():
    line = _unified(
        current_task=None,
        current_activity={"kind": "idle", "started_at": None},
    )

    assert "空闲" in line


# ---------------------------------------------------------------------------
# Elapsed time
# ---------------------------------------------------------------------------
#
# A wrong duration is worse than a missing one: an operator reading
# "已 8h10m" about work that started ten minutes ago has every reason to
# conclude the plan is stuck. The producer writes naive UTC, so the
# formatter has to know that.


def test_naive_timestamps_are_read_as_utc():
    """``ExecutionLogger`` stamps ``datetime.utcnow().isoformat()`` — naive
    UTC. Reading that against a naive local "now" is off by the machine's
    UTC offset, silently."""
    from datetime import datetime, timedelta

    from notifications.cards import _format_elapsed

    started = (datetime.utcnow() - timedelta(minutes=10)).isoformat()

    assert _format_elapsed(started) == "10m00s"


def test_aware_timestamps_are_honoured_as_written():
    from datetime import datetime, timedelta, timezone

    from notifications.cards import _format_elapsed

    started = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat().replace("+00:00", "Z")

    assert _format_elapsed(started) == "10m00s"


def test_elapsed_scales_to_hours():
    from datetime import datetime, timedelta

    from notifications.cards import _format_elapsed

    started = (datetime.utcnow() - timedelta(hours=2, minutes=5)).isoformat()

    assert _format_elapsed(started) == "2h05m"


def test_unusable_timestamps_render_nothing():
    """The suffix is a nicety. A card that cannot compute it must still
    render the activity rather than drop the line or invent a number."""
    from datetime import datetime, timedelta

    from notifications.cards import _activity_line, _format_elapsed

    assert _format_elapsed(None) == ""
    assert _format_elapsed("") == ""
    assert _format_elapsed("not-a-time") == ""
    # Future timestamp (clock skew between processes) — no negative.
    assert _format_elapsed(
        (datetime.utcnow() + timedelta(hours=1)).isoformat()
    ) == ""

    for bad in (None, "", "not-a-time"):
        line = _activity_line({"kind": "refine", "started_at": bad})
        assert "正在精化任务列表" in line, bad


def test_a_refine_line_carries_its_elapsed_time():
    from datetime import datetime, timedelta

    from notifications.cards import _activity_line

    started = (datetime.utcnow() - timedelta(minutes=4)).isoformat()
    line = _activity_line(
        {"kind": "refine", "started_at": started, "task_id": "9-9"},
    )

    assert "4m" in line
    assert "`9-9`" in line


# ---------------------------------------------------------------------------
# Watch fingerprint
# ---------------------------------------------------------------------------


def _progress(tasks, current=None, activity=None) -> dict:
    return {
        "tasks": tasks,
        "current": {"id": current} if current else None,
        "counts": {"total": len(tasks)},
        "current_activity": activity,
    }


_TASKS = [{"id": "1", "status": "completed"}]


def test_fingerprint_moves_when_a_refine_starts():
    """The whole point: no task row changes, so without the activity the
    watch would see nothing and the card would not be rebuilt."""
    before = FeishuNotifier._execution_state_fingerprint(
        _progress(_TASKS, activity={"kind": "idle", "started_at": "t0"}),
    )
    during = FeishuNotifier._execution_state_fingerprint(
        _progress(
            _TASKS,
            activity={"kind": "refine", "started_at": "2026-10-05T06:16:00"},
        ),
    )

    assert before != during


def test_fingerprint_moves_between_two_identical_kinds():
    """Two refines in a row are both real. Keying on the kind alone
    would make the second one invisible."""
    first = FeishuNotifier._execution_state_fingerprint(
        _progress(_TASKS, activity={"kind": "refine", "started_at": "t1"}),
    )
    second = FeishuNotifier._execution_state_fingerprint(
        _progress(_TASKS, activity={"kind": "refine", "started_at": "t2"}),
    )

    assert first != second


def test_fingerprint_is_stable_for_an_unchanged_payload():
    a = FeishuNotifier._execution_state_fingerprint(
        _progress(_TASKS, activity={"kind": "refine", "started_at": "t1"}),
    )
    b = FeishuNotifier._execution_state_fingerprint(
        _progress(_TASKS, activity={"kind": "refine", "started_at": "t1"}),
    )

    assert a == b


def test_fingerprint_tolerates_a_missing_activity_field():
    """Payloads from an older server (or a caller that builds its own)
    must not crash the watch."""
    assert FeishuNotifier._execution_state_fingerprint(
        _progress(_TASKS),
    ) is not None
