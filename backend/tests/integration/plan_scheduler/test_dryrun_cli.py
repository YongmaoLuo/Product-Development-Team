"""Integration tests: the dry-run diff CLI replays a recorded run and judges it.

What this file holds down
-------------------------
The native scheduler replaces the old cron scheduler, but only after a
24 hour dry run proves the two behave the same.  A 24 hour wait cannot
sit in a pull-request gate, so the *comparison* is replayed against a
recorded run instead: ``scripts/scheduler_dryrun_diff.py`` reads the
recorded ``ticks.jsonl`` and ``windows.json``, hands the windows to the
judge (``plan_scheduler.dryrun.evaluate_windows``), writes the
comparison out as ``diff.md``, and exits with the verdict.

The exit code is the contract a CI gate reads:

* **0** — the replay passed: 48 windows, at least 45 of them identical,
  the rest allowed-only, no forbidden difference anywhere;
* **1** — the replay did not pass: a forbidden difference, too few
  windows, or too few identical ones;
* **2** — the replay could not be run: an input file is missing, and the
  missing path is printed.

Two properties are as load-bearing as the exit code, and are pinned
here as well: the script writes ``diff.md`` and **nothing else** (it
never opens the scheduler database), and it never imports the engine
runtime — it is a judge, not a participant in the run it judges.

The report itself has to be readable by a human, because the 24 hour
gate is a human gate: every allowed difference gets its own line in
``diff.md``, and that line says where the written explanation for it
lives.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# The vocabulary the judge uses
# ---------------------------------------------------------------------------

#: The difference kinds the architecture calls allowed: a rhythm
#: difference in the set of running plans, and a difference in the order
#: two workspaces were served in.
ALLOWED_KINDS = ("transient_set_diff", "workspace_order_diff")

#: The difference kinds the architecture calls forbidden: the new engine
#: started a plan the old engine never started, the two engines
#: disagreed about evictability, and the two engines queued the plans in
#: the opposite order.
FORBIDDEN_KINDS = ("new_start", "eviction_flip", "queue_order_flip")

#: The number of windows a 24 hour run covers: 48 half-hour windows.
WINDOW_COUNT = 48

#: The first window of the recorded run the fixtures describe.
RUN_START = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cli_path() -> Path:
    """The CLI under test, found by walking up from this file.

    The script lives at the repository root's ``scripts/`` directory,
    not under ``backend/``, so the path is resolved from this file
    rather than from the current working directory.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "scripts" / "scheduler_dryrun_diff.py"
        if candidate.is_file():
            return candidate
    raise AssertionError("scripts/scheduler_dryrun_diff.py was not found")


def _start_of(index: int) -> str:
    """The timestamp of window ``index`` of the recorded run."""
    moment = RUN_START + timedelta(minutes=30 * index)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _windows(
    identical: int, allowed: int = 0, forbidden: int = 0
) -> List[Dict[str, Any]]:
    """Build the window records of a recorded run.

    The identical windows come first, then the allowed-only ones, then
    the forbidden one(s) — the shape of the list says nothing about the
    verdict, only the differences in it do.

    Every window says it was **compared**: the comparison is what found
    the agreement, and a window nobody compared agrees with nothing.
    """
    windows: List[Dict[str, Any]] = [
        {
            "index": index,
            "start": _start_of(index),
            "differences": [],
            "compared": True,
        }
        for index in range(WINDOW_COUNT)
    ]
    for offset in range(allowed):
        index = identical + offset
        kind = ALLOWED_KINDS[offset % len(ALLOWED_KINDS)]
        windows[index]["differences"] = [
            {"kind": kind, "explanation": f"第 {offset + 1} 条允许差异的书面解释"}
        ]
    for offset in range(forbidden):
        index = identical + allowed + offset
        kind = FORBIDDEN_KINDS[offset % len(FORBIDDEN_KINDS)]
        windows[index]["differences"] = [{"kind": kind}]
    return windows


def _uncompared_windows(count: int = WINDOW_COUNT) -> List[Dict[str, Any]]:
    """Build the window records of a run that compared nothing.

    This is the shape the production dry run leaves behind when no
    comparison side was provided: the new engine's snapshot of every
    window, with no difference and no comparison.
    """
    return [
        {
            "index": index,
            "start": _start_of(index),
            "running": [],
            "queue": [],
            "differences": [],
            "compared": False,
        }
        for index in range(count)
    ]


def _ticks(count: int = WINDOW_COUNT) -> List[Dict[str, Any]]:
    """The recorded tick decisions of the run — one per window."""
    return [
        {
            "ts": _start_of(index),
            "action": "admit",
            "plan_id": f"plan-{index:02d}",
            "reason": "waiting_for_slot",
        }
        for index in range(count)
    ]


def _write_ticks(path: Path, records: List[Dict[str, Any]]) -> Path:
    """Write the recorded ticks as JSONL."""
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _write_windows(path: Path, windows: List[Dict[str, Any]]) -> Path:
    """Write the window records as JSON."""
    path.write_text(
        json.dumps({"windows": windows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _run_cli(
    ticks: Path,
    windows: Path,
    out: Path,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Run the CLI the way a CI gate runs it."""
    return subprocess.run(
        [
            sys.executable,
            str(_cli_path()),
            "--ticks",
            str(ticks),
            "--windows",
            str(windows),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=None if cwd is None else str(cwd),
        env=env,
    )


# ---------------------------------------------------------------------------
# 1. The exit code is the verdict
# ---------------------------------------------------------------------------


def test_cli_passes_on_45_of_48(tmp_path: Path) -> None:
    """45 identical windows plus 3 allowed-only windows passes.

    This is the shape a healthy 24 hour run has: the two engines agree
    on 45 of the 48 windows, and the three they differ in differ only
    in ways the architecture calls allowed.
    """
    ticks = _write_ticks(tmp_path / "ticks.jsonl", _ticks())
    windows = _write_windows(
        tmp_path / "windows.json", _windows(identical=45, allowed=3)
    )
    out = tmp_path / "diff.md"

    result = _run_cli(ticks, windows, out)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASSED" in result.stdout
    assert out.is_file()


def test_cli_fails_on_forbidden(tmp_path: Path) -> None:
    """A single forbidden difference fails the whole replay.

    One forbidden difference is one too many: the switch does not happen
    on evidence that contains one, no matter how many of the other
    windows agree.
    """
    ticks = _write_ticks(tmp_path / "ticks.jsonl", _ticks())
    windows = _write_windows(
        tmp_path / "windows.json", _windows(identical=45, allowed=2, forbidden=1)
    )
    out = tmp_path / "diff.md"

    result = _run_cli(ticks, windows, out)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAILED" in result.stdout

    report = out.read_text(encoding="utf-8")
    assert "[forbidden]" in report
    assert FORBIDDEN_KINDS[0] in report


def test_cli_missing_file_exit_2(tmp_path: Path) -> None:
    """A missing input file exits 2 and prints the path that is missing.

    Exit 2 says "the replay did not run", which is a different thing
    from exit 1 ("the replay ran and the verdict is no").  A gate that
    cannot tell those apart would read a broken checkout as a failed
    dry run, and a failed dry run as a broken checkout.
    """
    ticks = _write_ticks(tmp_path / "ticks.jsonl", _ticks())
    missing = tmp_path / "windows.json"
    out = tmp_path / "diff.md"

    result = _run_cli(ticks, missing, out)

    assert result.returncode == 2, result.stdout + result.stderr
    assert str(missing) in result.stdout + result.stderr
    assert not out.exists()


def test_cli_fails_on_short_window_count(tmp_path: Path) -> None:
    """47 windows fail even though every one of them is identical.

    The comparison unit is the old engine's 30 minute window, and 24
    hours is 48 of them.  A run that stopped early has not produced the
    evidence the criterion asks for.
    """
    ticks = _write_ticks(tmp_path / "ticks.jsonl", _ticks())
    windows = _write_windows(
        tmp_path / "windows.json", _windows(identical=45, allowed=3)[:47]
    )
    out = tmp_path / "diff.md"

    result = _run_cli(ticks, windows, out)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAILED" in result.stdout


def test_cli_fails_on_uncompared_windows(tmp_path: Path) -> None:
    """A run that compared nothing fails the gate.

    The production dry run leaves the new engine's side of every
    window behind.  Until the old engine's side is compared with it,
    those windows carry no verdict — and a gate that let them through
    would switch on a run that compared nothing at all.
    """
    ticks = _write_ticks(tmp_path / "ticks.jsonl", _ticks())
    windows = _write_windows(tmp_path / "windows.json", _uncompared_windows())
    out = tmp_path / "diff.md"

    result = _run_cli(ticks, windows, out)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAILED" in result.stdout

    report = out.read_text(encoding="utf-8")
    assert "未对照" in report, (
        "the report does not say that the windows were never compared"
    )


def test_cli_fails_on_one_uncompared_window(tmp_path: Path) -> None:
    """One window nobody compared is enough to stop the switch."""
    windows = _windows(identical=45, allowed=2)
    windows[47] = {
        "index": 47,
        "start": _start_of(47),
        "differences": [],
        "compared": False,
    }

    ticks = _write_ticks(tmp_path / "ticks.jsonl", _ticks())
    path = _write_windows(tmp_path / "windows.json", windows)
    out = tmp_path / "diff.md"

    result = _run_cli(ticks, path, out)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAILED" in result.stdout


def test_cli_malformed_window_exit_2(tmp_path: Path) -> None:
    """A window the judge cannot read exits 2, not 1.

    Exit 1 means "the replay ran and the verdict is no".  A record the
    judge cannot even read is not a verdict — the replay never ran, so
    the gate has to be told the difference.
    """
    ticks = _write_ticks(tmp_path / "ticks.jsonl", _ticks())
    windows = tmp_path / "windows.json"
    windows.write_text(
        json.dumps({"windows": [{"differences": 7}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    out = tmp_path / "diff.md"

    result = _run_cli(ticks, windows, out)

    assert result.returncode == 2, result.stdout + result.stderr
    assert not out.exists()


# ---------------------------------------------------------------------------
# 2. The report is the evidence a human reads
# ---------------------------------------------------------------------------


def test_cli_reads_the_production_windows_shape(tmp_path: Path) -> None:
    """The production ``windows.json`` is a top-level list — the CLI reads it.

    The writer writes the day's windows as a plain list, and the gate
    is replayed against exactly that file.
    """
    ticks = _write_ticks(tmp_path / "ticks.jsonl", _ticks())
    windows = tmp_path / "windows.json"
    windows.write_text(
        json.dumps(_windows(identical=45, allowed=3), ensure_ascii=False),
        encoding="utf-8",
    )
    out = tmp_path / "diff.md"

    result = _run_cli(ticks, windows, out)

    assert result.returncode == 0, result.stdout + result.stderr
    assert out.is_file()


def test_diff_md_lists_each_allowed_difference(tmp_path: Path) -> None:
    """Every allowed difference gets its own line, saying where its
    explanation lives.

    The criterion is not just "the differences are allowed" but "every
    allowed difference is explained in writing".  A report that lists
    the differences without pointing at their explanations leaves the
    second half of the criterion unaudited.
    """
    ticks = _write_ticks(tmp_path / "ticks.jsonl", _ticks())
    windows = _write_windows(
        tmp_path / "windows.json", _windows(identical=45, allowed=3)
    )
    out = tmp_path / "diff.md"

    result = _run_cli(ticks, windows, out)
    assert result.returncode == 0, result.stdout + result.stderr

    report = out.read_text(encoding="utf-8")
    lines = [
        line
        for line in report.splitlines()
        if line.startswith("- [allowed]")
    ]

    assert len(lines) == 3
    for kind in ALLOWED_KINDS:
        assert any(kind in line for line in lines)
    for line in lines:
        assert "windows.json#" in line


def test_diff_md_lists_each_window(tmp_path: Path) -> None:
    """The report holds one conclusion per window, not a summary only.

    A summary cannot be checked against the recorded run; the
    per-window conclusion can.
    """
    ticks = _write_ticks(tmp_path / "ticks.jsonl", _ticks())
    windows = _write_windows(
        tmp_path / "windows.json", _windows(identical=45, allowed=3)
    )
    out = tmp_path / "diff.md"

    result = _run_cli(ticks, windows, out)
    assert result.returncode == 0, result.stdout + result.stderr

    report = out.read_text(encoding="utf-8")

    for index in range(WINDOW_COUNT):
        assert f"周期 {index + 1}" in report


# ---------------------------------------------------------------------------
# 3. The script is a judge, not a participant
# ---------------------------------------------------------------------------


def test_cli_writes_no_scheduler_database(tmp_path: Path) -> None:
    """The CLI writes diff.md and nothing else.

    The scheduler's state database is the live system's bookkeeping.
    A comparison tool that opened it would be a writer in the run it is
    judging, and the comparison would no longer be read-only evidence.
    """
    db_path = tmp_path / "state.db"
    env = dict(os.environ)
    env["PDT_STATE_DB_PATH"] = str(db_path)

    ticks = _write_ticks(tmp_path / "ticks.jsonl", _ticks())
    windows = _write_windows(
        tmp_path / "windows.json", _windows(identical=45, allowed=3)
    )
    out = tmp_path / "diff.md"

    result = _run_cli(ticks, windows, out, cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert out.is_file()
    assert not db_path.exists()
    assert list(tmp_path.glob("*.db")) == []


def test_cli_imports_no_engine_runtime() -> None:
    """The script never imports the engine, the store or a subprocess.

    The judgement is made by two pure functions; anything that reaches
    for the engine runtime, the scheduler database or a child process
    would make the replay a participant in the run it is judging.
    """
    tree = ast.parse(_cli_path().read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))

    forbidden = {
        "plan_scheduler.store",
        "plan_scheduler.engine",
        "plan_scheduler.agent",
        "plan_scheduler.policy",
        "sqlite3",
        "subprocess",
        "threading",
        "socket",
        "requests",
        "httpx",
    }

    assert not (imported & forbidden), sorted(imported & forbidden)
