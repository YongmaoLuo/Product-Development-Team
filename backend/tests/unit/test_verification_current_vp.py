"""2026-09-14: the progress endpoint must report CURRENT-ROUND VP
activity as live truth, not inherit stale verdicts.

The card showed "验证中" with no current-VP identity
("黑盒"), and the progress payload misclassified a re-running VP as
failed because the persisted ``plan_verification.progress_state``
still carried the previous (interrupted) round's verdicts.

``_read_latest_round_vp_activity`` scans ONLY the newest
``verification_*.log``; a ``vp_start`` without a subsequent
``vp_complete`` in that file means the VP is mid-flight right now.
``_build_verification_progress`` uses it to:

  * remove such VPs from the persisted completed/failed/skipped lists,
  * include them in ``running_vp_ids`` (so ``counts.in_progress`` and
    the per-VP ``status="running"`` are truthful), and
  * fall back to the earliest-started running VP for ``current_vp``
    when the executor's persisted "primary" VP is absent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import server as _server_mod


def _write_log(plan_dir: Path, name: str, entries: list) -> Path:
    logs = plan_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    p = logs / name
    with open(p, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return p


def _ev(event_type, vp, ts):
    return {
        "verification_point_id": vp,
        "event_type": event_type,
        "timestamp": ts,
    }


@pytest.fixture
def plan_dir(tmp_path):
    d = tmp_path / "plan-vp"
    d.mkdir()
    return d


def test_activity_reads_only_newest_round_log(plan_dir):
    _write_log(plan_dir, "verification_1_20260913_120000.log", [
        _ev("vp_start", "VP-001", "2026-09-13T12:00:00"),
        _ev("vp_complete", "VP-001", "2026-09-13T12:05:00"),
    ])
    newest = _write_log(plan_dir, "verification_2_20260914_120000.log", [
        _ev("vp_start", "VP-023", "2026-09-14T12:00:00"),
    ])
    # Make mtime ordering deterministic regardless of write speed.
    old = plan_dir / "logs" / "verification_1_20260913_120000.log"
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(newest, (2_000_000, 2_000_000))

    activity = _server_mod._read_latest_round_vp_activity(plan_dir)
    assert set(activity) == {"VP-023"}, (
        f"only the newest round log is scanned; got {set(activity)}"
    )
    assert activity["VP-023"]["start"] == "2026-09-14T12:00:00"
    assert activity["VP-023"]["complete"] == ""


def test_running_vps_require_start_without_complete(plan_dir):
    _write_log(plan_dir, "verification_2_20260914_120000.log", [
        _ev("vp_start", "VP-023", "2026-09-14T12:00:00"),
        _ev("vp_start", "VP-006", "2026-09-14T12:01:00"),
        _ev("vp_complete", "VP-006", "2026-09-14T12:04:00"),
    ])
    activity = _server_mod._read_latest_round_vp_activity(plan_dir)
    running = _server_mod._running_vps_from_activity(activity)
    assert running == {"VP-023"}


def test_no_logs_returns_empty(plan_dir):
    assert _server_mod._read_latest_round_vp_activity(plan_dir) == {}
    assert _server_mod._running_vps_from_activity({}) == set()
