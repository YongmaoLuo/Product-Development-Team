"""TDD spec for the watchdog entry point + dead-loop detection.

Architecture decision point 6 defines the watchdog as an **independent
process** whose trigger condition is a machine-decidable dead loop:

    在 task 状态存储中无任何 task 状态推进（无 status / commit_sha 变更）
    的前提下，daemon 重复进入同一 plan_id 下同一 task_id 的执行循环，
    计 1 次；连续计数 ≥3 次即触发 watchdog。

This module (task 12) covers the *detection* half only — the entry
point, the error-fingerprint accumulator and the threshold check.
The action sequence (LLM 调研 → auto-fix → validate-through → 重启)
is task 13 and hangs off the ``on_trigger`` seam exercised here.

The three pieces under test:

* ``compute_fingerprint(plan_id, task_id, error_text)`` — a stable
  sha256 digest that collapses volatile substrings (timestamps, hex
  addresses, UUIDs, durations) so the *same* failure reported three
  times in a row produces the *same* fingerprint.
* ``compute_progress_token(tasks)`` — a digest over every row's
  ``(id, status, commit_sha)`` triple. Any state progression changes
  the token, which resets the accumulator (progress ⇒ not a dead loop).
* ``DeadLoopDetector`` / ``Watchdog`` — accumulate consecutive
  identical fingerprints per plan and flip ``triggered`` at
  ``DEADLOOP_THRESHOLD`` (3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework.ids import InvalidPlanIdError
from watchdog import (
    DEADLOOP_THRESHOLD,
    DeadLoopDetector,
    DetectionResult,
    Watchdog,
    compute_fingerprint,
    compute_progress_token,
    main,
    normalise_error,
)


PLAN_ID = "20260807-watchdog-detect-spec"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_log(plan_dir: Path, entries: list[dict]) -> Path:
    """Write ``entries`` as JSON-lines into ``plan_dir/execution.log``."""
    log_file = plan_dir / "execution.log"
    with log_file.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log_file


def _append_log(plan_dir: Path, entry: dict) -> None:
    """Append a single JSON-line entry to ``plan_dir/execution.log``."""
    with (plan_dir / "execution.log").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _error_entry(task_id: str, message: str, ts: str = "2026-08-07T10:00:00") -> dict:
    return {
        "ts": ts,
        "level": "ERROR",
        "event": "task_failed",
        "message": message,
        "task_id": task_id,
    }


def _write_tasks(plan_dir: Path, tasks: list[dict]) -> Path:
    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text(
        json.dumps({"requirement": "spec", "tasks": tasks}, ensure_ascii=False),
        encoding="utf-8",
    )
    return tasks_file


@pytest.fixture
def plans_root(tmp_path: Path) -> Path:
    """A hermetic ``plans/`` root with one plan dir already created."""
    root = tmp_path / "plans"
    (root / PLAN_ID).mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# 1. normalise_error — volatile substrings are collapsed
# ---------------------------------------------------------------------------


def test_normalise_error_collapses_timestamps() -> None:
    a = normalise_error("failed at 2026-08-07T10:00:00.123456+00:00 in step 2")
    b = normalise_error("failed at 2026-08-07T23:59:59.999999+00:00 in step 2")
    assert a == b


def test_normalise_error_collapses_hex_addresses() -> None:
    a = normalise_error("<TaskRunner object at 0x7fdeadbeef>")
    b = normalise_error("<TaskRunner object at 0x7f00c0ffee>")
    assert a == b


def test_normalise_error_collapses_uuids() -> None:
    a = normalise_error("run 3f2504e0-4f89-11d3-9a0c-0305e82c3301 aborted")
    b = normalise_error("run 550e8400-e29b-41d4-a716-446655440000 aborted")
    assert a == b


def test_normalise_error_collapses_durations() -> None:
    a = normalise_error("timed out after 1800.42s")
    b = normalise_error("timed out after 12.7s")
    assert a == b


def test_normalise_error_preserves_distinct_messages() -> None:
    assert normalise_error("ImportError: no module x") != normalise_error(
        "AssertionError: expected 3"
    )


def test_normalise_error_is_whitespace_insensitive() -> None:
    assert normalise_error("a   b\n c") == normalise_error("a b c")


# ---------------------------------------------------------------------------
# 2. compute_fingerprint — stable, scoped, sha256-shaped
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_across_calls() -> None:
    first = compute_fingerprint(PLAN_ID, "12", "boom")
    second = compute_fingerprint(PLAN_ID, "12", "boom")
    assert first == second


def test_fingerprint_is_sha256_hex() -> None:
    fp = compute_fingerprint(PLAN_ID, "12", "boom")
    assert len(fp) == 64
    assert set(fp) <= set("0123456789abcdef")


def test_fingerprint_ignores_volatile_substrings() -> None:
    a = compute_fingerprint(PLAN_ID, "12", "boom at 2026-08-07T10:00:00")
    b = compute_fingerprint(PLAN_ID, "12", "boom at 2026-08-08T20:30:00")
    assert a == b


def test_fingerprint_differs_per_task_id() -> None:
    assert compute_fingerprint(PLAN_ID, "12", "boom") != compute_fingerprint(
        PLAN_ID, "13", "boom"
    )


def test_fingerprint_differs_per_plan_id() -> None:
    assert compute_fingerprint(PLAN_ID, "12", "boom") != compute_fingerprint(
        "20260807-other-plan", "12", "boom"
    )


def test_fingerprint_differs_per_error_text() -> None:
    assert compute_fingerprint(PLAN_ID, "12", "boom") != compute_fingerprint(
        PLAN_ID, "12", "bang"
    )


def test_fingerprint_rejects_unsafe_plan_id() -> None:
    with pytest.raises(InvalidPlanIdError):
        compute_fingerprint("../../etc/passwd", "12", "boom")


# ---------------------------------------------------------------------------
# 3. compute_progress_token — status / commit_sha progression
# ---------------------------------------------------------------------------


def test_progress_token_stable_for_identical_snapshot() -> None:
    rows = [{"id": "1", "status": "in_progress", "commit_sha": None}]
    assert compute_progress_token(rows) == compute_progress_token(list(rows))


def test_progress_token_changes_on_status_change() -> None:
    before = compute_progress_token([{"id": "1", "status": "in_progress"}])
    after = compute_progress_token([{"id": "1", "status": "completed"}])
    assert before != after


def test_progress_token_changes_on_commit_sha_change() -> None:
    before = compute_progress_token([{"id": "1", "status": "x", "commit_sha": None}])
    after = compute_progress_token([{"id": "1", "status": "x", "commit_sha": "abc123"}])
    assert before != after


def test_progress_token_ignores_row_order() -> None:
    a = [{"id": "1", "status": "p"}, {"id": "2", "status": "q"}]
    b = [{"id": "2", "status": "q"}, {"id": "1", "status": "p"}]
    assert compute_progress_token(a) == compute_progress_token(b)


def test_progress_token_ignores_non_state_fields() -> None:
    a = [{"id": "1", "status": "p", "title": "before"}]
    b = [{"id": "1", "status": "p", "title": "after"}]
    assert compute_progress_token(a) == compute_progress_token(b)


def test_progress_token_of_empty_snapshot_is_stable() -> None:
    assert compute_progress_token([]) == compute_progress_token([])


# ---------------------------------------------------------------------------
# 4. DeadLoopDetector — accumulation + threshold
# ---------------------------------------------------------------------------


def test_threshold_constant_is_three() -> None:
    assert DEADLOOP_THRESHOLD == 3


def test_first_record_is_not_triggered() -> None:
    det = DeadLoopDetector()
    result = det.record(PLAN_ID, "12", "boom", progress_token="t0")
    assert isinstance(result, DetectionResult)
    assert result.count == 1
    assert result.triggered is False


def test_second_record_is_not_triggered() -> None:
    det = DeadLoopDetector()
    det.record(PLAN_ID, "12", "boom", progress_token="t0")
    result = det.record(PLAN_ID, "12", "boom", progress_token="t0")
    assert result.count == 2
    assert result.triggered is False


def test_third_identical_record_triggers() -> None:
    det = DeadLoopDetector()
    for _ in range(2):
        det.record(PLAN_ID, "12", "boom", progress_token="t0")
    result = det.record(PLAN_ID, "12", "boom", progress_token="t0")
    assert result.count == 3
    assert result.triggered is True
    assert result.threshold == DEADLOOP_THRESHOLD
    assert result.task_id == "12"
    assert "3" in result.reason


def test_counter_keeps_growing_after_trigger() -> None:
    det = DeadLoopDetector()
    for _ in range(3):
        det.record(PLAN_ID, "12", "boom", progress_token="t0")
    result = det.record(PLAN_ID, "12", "boom", progress_token="t0")
    assert result.count == 4
    assert result.triggered is True


def test_different_error_resets_counter() -> None:
    det = DeadLoopDetector()
    det.record(PLAN_ID, "12", "boom", progress_token="t0")
    det.record(PLAN_ID, "12", "boom", progress_token="t0")
    result = det.record(PLAN_ID, "12", "a different failure", progress_token="t0")
    assert result.count == 1
    assert result.triggered is False


def test_different_task_id_resets_counter() -> None:
    det = DeadLoopDetector()
    det.record(PLAN_ID, "12", "boom", progress_token="t0")
    det.record(PLAN_ID, "12", "boom", progress_token="t0")
    result = det.record(PLAN_ID, "13", "boom", progress_token="t0")
    assert result.count == 1
    assert result.triggered is False


def test_state_progression_resets_counter() -> None:
    """A changed progress token means the daemon advanced — not a dead loop."""
    det = DeadLoopDetector()
    det.record(PLAN_ID, "12", "boom", progress_token="t0")
    det.record(PLAN_ID, "12", "boom", progress_token="t0")
    result = det.record(PLAN_ID, "12", "boom", progress_token="t1")
    assert result.count == 1
    assert result.triggered is False


def test_plans_are_tracked_independently() -> None:
    det = DeadLoopDetector()
    other = "20260807-other-plan"
    for _ in range(3):
        det.record(PLAN_ID, "12", "boom", progress_token="t0")
    result = det.record(other, "12", "boom", progress_token="t0")
    assert result.count == 1
    assert result.triggered is False
    assert det.count(PLAN_ID) == 3


def test_reset_clears_a_single_plan() -> None:
    det = DeadLoopDetector()
    det.record(PLAN_ID, "12", "boom", progress_token="t0")
    det.reset(PLAN_ID)
    assert det.count(PLAN_ID) == 0
    assert det.record(PLAN_ID, "12", "boom", progress_token="t0").count == 1


def test_reset_without_plan_clears_everything() -> None:
    det = DeadLoopDetector()
    det.record(PLAN_ID, "12", "boom", progress_token="t0")
    det.record("20260807-other-plan", "12", "boom", progress_token="t0")
    det.reset()
    assert det.count(PLAN_ID) == 0
    assert det.count("20260807-other-plan") == 0


def test_custom_threshold_is_honoured() -> None:
    det = DeadLoopDetector(threshold=2)
    det.record(PLAN_ID, "12", "boom", progress_token="t0")
    result = det.record(PLAN_ID, "12", "boom", progress_token="t0")
    assert result.triggered is True
    assert result.threshold == 2


def test_detector_rejects_non_positive_threshold() -> None:
    with pytest.raises(ValueError):
        DeadLoopDetector(threshold=0)


def test_detector_rejects_unsafe_plan_id() -> None:
    det = DeadLoopDetector()
    with pytest.raises(InvalidPlanIdError):
        det.record("../escape", "12", "boom", progress_token="t0")


def test_detection_result_carries_observed_at_timestamp() -> None:
    det = DeadLoopDetector()
    result = det.record(PLAN_ID, "12", "boom", progress_token="t0")
    assert result.observed_at.endswith("+00:00")


def test_detection_result_is_json_serialisable() -> None:
    det = DeadLoopDetector()
    result = det.record(PLAN_ID, "12", "boom", progress_token="t0")
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["plan_id"] == PLAN_ID
    assert payload["count"] == 1
    assert payload["triggered"] is False


# ---------------------------------------------------------------------------
# 5. Watchdog — entry point over the on-disk plan directory
# ---------------------------------------------------------------------------


def test_watchdog_returns_none_without_execution_log(plans_root: Path) -> None:
    wd = Watchdog(PLAN_ID, plans_root=plans_root)
    assert wd.detect_once() is None


def test_watchdog_returns_none_when_log_has_no_errors(plans_root: Path) -> None:
    plan_dir = plans_root / PLAN_ID
    _write_log(
        plan_dir,
        [{"ts": "2026-08-07T10:00:00", "level": "INFO", "event": "task_started",
          "message": "go", "task_id": "12"}],
    )
    wd = Watchdog(PLAN_ID, plans_root=plans_root)
    assert wd.detect_once() is None


def test_watchdog_consumes_only_new_error_entries(plans_root: Path) -> None:
    """Re-polling an unchanged log must NOT re-count the same failure."""
    plan_dir = plans_root / PLAN_ID
    _write_log(plan_dir, [_error_entry("12", "boom")])
    wd = Watchdog(PLAN_ID, plans_root=plans_root)

    first = wd.detect_once()
    assert first is not None and first.count == 1
    # Second poll: nothing new appended → no fresh observation.
    assert wd.detect_once() is None


def test_watchdog_triggers_after_three_identical_failures(plans_root: Path) -> None:
    plan_dir = plans_root / PLAN_ID
    _write_tasks(plan_dir, [{"id": "12", "status": "in_progress", "commit_sha": None}])
    _write_log(plan_dir, [_error_entry("12", "boom at 2026-08-07T10:00:00")])
    wd = Watchdog(PLAN_ID, plans_root=plans_root)

    assert wd.detect_once().triggered is False
    _append_log(plan_dir, _error_entry("12", "boom at 2026-08-07T11:11:11"))
    assert wd.detect_once().triggered is False
    _append_log(plan_dir, _error_entry("12", "boom at 2026-08-07T12:22:22"))
    final = wd.detect_once()
    assert final.triggered is True
    assert final.count == 3


def test_watchdog_does_not_trigger_when_status_progresses(plans_root: Path) -> None:
    plan_dir = plans_root / PLAN_ID
    _write_tasks(plan_dir, [{"id": "12", "status": "in_progress"}])
    _write_log(plan_dir, [_error_entry("12", "boom")])
    wd = Watchdog(PLAN_ID, plans_root=plans_root)

    assert wd.detect_once().count == 1
    _append_log(plan_dir, _error_entry("12", "boom"))
    assert wd.detect_once().count == 2
    # The daemon advanced the task before failing again → counter resets.
    _write_tasks(plan_dir, [{"id": "12", "status": "completed", "commit_sha": "abc"}])
    _append_log(plan_dir, _error_entry("12", "boom"))
    result = wd.detect_once()
    assert result.count == 1
    assert result.triggered is False


def test_watchdog_handles_multiple_new_entries_in_one_poll(plans_root: Path) -> None:
    plan_dir = plans_root / PLAN_ID
    _write_log(
        plan_dir,
        [_error_entry("12", "boom"), _error_entry("12", "boom"), _error_entry("12", "boom")],
    )
    wd = Watchdog(PLAN_ID, plans_root=plans_root)
    result = wd.detect_once()
    assert result.count == 3
    assert result.triggered is True


def test_watchdog_skips_malformed_log_lines(plans_root: Path) -> None:
    plan_dir = plans_root / PLAN_ID
    log_file = plan_dir / "execution.log"
    log_file.write_text(
        "not json\n"
        + json.dumps(_error_entry("12", "boom"))
        + "\n\n{broken\n",
        encoding="utf-8",
    )
    wd = Watchdog(PLAN_ID, plans_root=plans_root)
    result = wd.detect_once()
    assert result is not None
    assert result.count == 1


def test_watchdog_recognises_critical_and_api_error_events(plans_root: Path) -> None:
    plan_dir = plans_root / PLAN_ID
    _write_log(
        plan_dir,
        [
            {"ts": "t", "level": "CRITICAL", "event": "loop_detected",
             "message": "boom", "task_id": "12"},
            {"ts": "t", "level": "INFO", "event": "task_api_error",
             "message": "boom", "task_id": "12"},
            {"ts": "t", "level": "ERROR", "event": "task_timeout",
             "message": "boom", "task_id": "12"},
        ],
    )
    wd = Watchdog(PLAN_ID, plans_root=plans_root)
    result = wd.detect_once()
    assert result.count == 3
    assert result.triggered is True


def test_watchdog_invokes_on_trigger_hook_once_per_trigger(plans_root: Path) -> None:
    """The ``on_trigger`` seam is where task 13 hangs the action sequence."""
    plan_dir = plans_root / PLAN_ID
    calls: list[DetectionResult] = []
    _write_log(plan_dir, [_error_entry("12", "boom")] * 3)
    wd = Watchdog(PLAN_ID, plans_root=plans_root, on_trigger=calls.append)
    wd.detect_once()
    assert len(calls) == 1
    assert calls[0].triggered is True


def test_watchdog_does_not_invoke_hook_below_threshold(plans_root: Path) -> None:
    plan_dir = plans_root / PLAN_ID
    calls: list[DetectionResult] = []
    _write_log(plan_dir, [_error_entry("12", "boom")] * 2)
    wd = Watchdog(PLAN_ID, plans_root=plans_root, on_trigger=calls.append)
    wd.detect_once()
    assert calls == []


def test_watchdog_rejects_unsafe_plan_id(plans_root: Path) -> None:
    with pytest.raises(InvalidPlanIdError):
        Watchdog("../../etc", plans_root=plans_root)


def test_watchdog_missing_tasks_file_still_detects(plans_root: Path) -> None:
    """A missing tasks.json yields a constant progress token, not a crash."""
    plan_dir = plans_root / PLAN_ID
    _write_log(plan_dir, [_error_entry("12", "boom")] * 3)
    wd = Watchdog(PLAN_ID, plans_root=plans_root)
    assert wd.detect_once().triggered is True


def test_watchdog_tolerates_corrupt_tasks_file(plans_root: Path) -> None:
    plan_dir = plans_root / PLAN_ID
    (plan_dir / "tasks.json").write_text("{not json", encoding="utf-8")
    _write_log(plan_dir, [_error_entry("12", "boom")] * 3)
    wd = Watchdog(PLAN_ID, plans_root=plans_root)
    assert wd.detect_once().triggered is True


def test_watchdog_accepts_legacy_bare_list_tasks_file(plans_root: Path) -> None:
    plan_dir = plans_root / PLAN_ID
    (plan_dir / "tasks.json").write_text(
        json.dumps([{"id": "12", "status": "in_progress"}]), encoding="utf-8"
    )
    _write_log(plan_dir, [_error_entry("12", "boom")])
    wd = Watchdog(PLAN_ID, plans_root=plans_root)
    assert wd.detect_once().count == 1


# ---------------------------------------------------------------------------
# 6. main() — the independent-process CLI entry point
# ---------------------------------------------------------------------------


def test_main_exits_zero_when_healthy(plans_root: Path, capsys) -> None:
    plan_dir = plans_root / PLAN_ID
    _write_log(plan_dir, [_error_entry("12", "boom")])
    code = main(["--plan-id", PLAN_ID, "--plans-root", str(plans_root), "--once"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["triggered"] is False
    assert payload["count"] == 1


def test_main_exits_one_when_dead_loop_triggered(plans_root: Path, capsys) -> None:
    plan_dir = plans_root / PLAN_ID
    _write_log(plan_dir, [_error_entry("12", "boom")] * 3)
    code = main(["--plan-id", PLAN_ID, "--plans-root", str(plans_root), "--once"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["triggered"] is True
    assert payload["count"] == 3


def test_main_reports_no_observation(plans_root: Path, capsys) -> None:
    code = main(["--plan-id", PLAN_ID, "--plans-root", str(plans_root), "--once"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["triggered"] is False
    assert payload["observation"] is None


def test_main_rejects_unsafe_plan_id(plans_root: Path, capsys) -> None:
    code = main(["--plan-id", "../../etc", "--plans-root", str(plans_root), "--once"])
    assert code == 2
    assert "plan_id" in capsys.readouterr().err


def test_main_honours_custom_threshold(plans_root: Path, capsys) -> None:
    plan_dir = plans_root / PLAN_ID
    _write_log(plan_dir, [_error_entry("12", "boom")] * 2)
    code = main([
        "--plan-id", PLAN_ID,
        "--plans-root", str(plans_root),
        "--threshold", "2",
        "--once",
    ])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["threshold"] == 2


def test_main_rejects_non_positive_threshold(plans_root: Path, capsys) -> None:
    code = main([
        "--plan-id", PLAN_ID,
        "--plans-root", str(plans_root),
        "--threshold", "0",
        "--once",
    ])
    assert code == 2
    assert "threshold" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 7. Structural guards — the module must stay decoupled
# ---------------------------------------------------------------------------


def test_module_uses_framework_clock_not_datetime_now() -> None:
    """Decision point 5: every timestamp flows through framework.clock."""
    source = (Path(__file__).resolve().parents[2] / "watchdog.py").read_text(
        encoding="utf-8"
    )
    assert "datetime.now(" not in source
    assert "utcnow_iso" in source


def test_module_does_not_import_dispatcher() -> None:
    """The watchdog is an independent process — no daemon imports."""
    source = (Path(__file__).resolve().parents[2] / "watchdog.py").read_text(
        encoding="utf-8"
    )
    assert "import agent" not in source
    assert "from agent import" not in source
