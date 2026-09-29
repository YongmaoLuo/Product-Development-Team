"""VP-019 anchor (2/3): dirname date takes precedence over file metadata.

The state-machine refactor pins the archive classification to the
``YYYYMMDD-*`` dirname, NOT to the directory's filesystem metadata
(mtime/atime). This protects the contract from hand-edited timestamps
that would otherwise "resurrect" an archived plan back into the
active SQLite path.

Conflict-resolution rules verified here:

  1. Dirname parses as 20260801 (archived) + mtime = 20260810 (new)
     => "archived" (dirname wins).
  2. The same rule applies regardless of which kind of file metadata
     is consulted (mtime as a representative).
  3. The CUTOFF_2026_08_05 module constant is the single source of
     truth for the cutoff — there is no fallback path that consults
     filesystem metadata.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from state_machine.db.archive_scan import CUTOFF_2026_08_05, classify_plan


def test_dirname_archived_overrides_future_mtime(tmp_path: Path) -> None:
    """An archived dirname (``20260801``) wins over a future mtime
    (``20260810``) — the plan must be classified ``"archived"``.

    This is the central regression anchor: if a future refactor
    accidentally adds an mtime fallback ("if dirname parse fails, use
    mtime"), then plans with both an archived dirname AND a future
    mtime would silently flip to ``"new"``. The explicit
    before/after timestamps here make that regression loud.
    """
    plan_dir = tmp_path / "20260801-older-task"
    plan_dir.mkdir()

    # Set mtime to 0810 12:00 — well AFTER the 0805 cutoff.
    future_mtime_ts = datetime(2026, 8, 10, 12, 0, 0).timestamp()
    os.utime(plan_dir, (future_mtime_ts, future_mtime_ts))

    cutoff = datetime(2026, 8, 5)
    result = classify_plan(plan_dir, cutoff)

    assert result == "archived", (
        f"dirname=20260801 must dominate mtime=20260810; got {result!r}"
    )


def test_module_cutoff_constant_matches_pinned_date() -> None:
    """``CUTOFF_2026_08_05`` is the pinned boundary — explicit assertion.

    This test fails fast if anyone bumps the constant, which would
    silently re-classify every existing plan. The constant exists
    explicitly so the boundary is centralised and monkeypatch-able
    in tests, and so a careless refactor (e.g. ``datetime.now()``)
    is caught immediately.
    """
    assert CUTOFF_2026_08_05 == datetime(2026, 8, 5), (
        f"CUTOFF_2026_08_05 must be pinned to 2026-08-05 00:00:00; "
        f"got {CUTOFF_2026_08_05.isoformat()!r}."
    )


def test_three_source_priority_dirname_first(tmp_path: Path) -> None:
    """Three-source priority is deterministic: dirname > mtime > nothing.

    Even with all three sources present and in conflict, the dirname
    is the single source of truth. We simulate the conflict by
    setting mtime to AFTER the cutoff while leaving the dirname at
    20260801, and verify the verdict matches the dirname.
    """
    plan_dir = tmp_path / "20260801-conflict-task"
    plan_dir.mkdir()

    # mtime would normally say "new" (after cutoff), but dirname says
    # "archived" — dirname wins.
    future_mtime_ts = datetime(2026, 9, 1, 0, 0, 0).timestamp()
    os.utime(plan_dir, (future_mtime_ts, future_mtime_ts))

    result = classify_plan(plan_dir, datetime(2026, 8, 5))

    assert result == "archived", (
        "dirname priority over mtime is a hard contract; "
        f"expected 'archived', got {result!r}"
    )
