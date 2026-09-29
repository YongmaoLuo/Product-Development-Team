"""The progress endpoint must not read a stale legacy task list.

Background
----------
``GET /api/execution/{plan_id}/progress`` builds its response from
``plans/<id>/tasks.json`` — but since 2026-09-06 it also looks for a
legacy ``tasks_with_repair_round_<N>.json`` (the merged snapshot the
pre-2026-09-08 repair pipeline used to write) and used to prefer it
**unconditionally** whenever one existed.

That is a split-brain: the executor reads ``plans/<id>/tasks.json``
(``cli.py --tasks-file``) while the card read the legacy file. The two
disagreed for any plan whose legacy snapshot predated its current
``tasks.json``. Observed on
``2026-09-04 plan`` (2026-09-14):

  * the legacy ``tasks_with_repair_round_3.json`` (mtime 09-08) still
    carried ``R3-2`` — a task present neither in ``state.db`` nor in
    ``tasks.json``, so unrunnable — and the card showed it ``pending``
    forever;
  * ``R1-5``, re-queued into ``tasks.json`` on 09-14, was invisible
    because the legacy snapshot predates it.

The fix decides by mtime, because the legacy file is legitimate for
plans whose repair pipeline ran *before* the 2026-09-08 single-writer
refactor — those plans have no newer ``tasks.json`` to prefer.

Contract pinned here:

  1. both files present, ``tasks.json`` newer → ``tasks.json`` wins;
  2. both files present, legacy newer → the legacy file wins (the
     backwards-compat path must survive);
  3. either way the state.db orphan merge still runs on top.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

PLAN_ID = "plan-legacy-tasks-file"


def _write_json(path: Path, tasks: list[dict], *, age_seconds: float) -> None:
    """Write ``tasks`` with an explicit mtime offset from now."""
    path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    when = time.time() - age_seconds
    os.utime(path, (when, when))


def _task(tid: str) -> dict:
    return {"id": tid, "title": tid, "description": tid, "status": "pending"}


def _setup(tmp_path, monkeypatch, *, legacy_is_newer: bool) -> Path:
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # Canonical list: R1-5 (re-queued) — the legacy snapshot predates it.
    _write_json(
        plan_dir / "tasks.json",
        [_task("1"), _task("2")],
        age_seconds=0 if not legacy_is_newer else 600,
    )
    # Legacy merged snapshot: carries the ghost R3-2 the executor can
    # never run (it is in neither tasks.json nor state.db).
    _write_json(
        plan_dir / "tasks_with_repair_round_3.json",
        [_task("1"), _task("2"), _task("R3-2")],
        age_seconds=600 if not legacy_is_newer else 0,
    )

    import server

    monkeypatch.setattr(
        server, "PLANS_DIR", plans_dir, raising=False
    )
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_project_dir", lambda pid: project_dir)
    return plan_dir


def _progress_task_ids(tmp_path, monkeypatch, *, legacy_is_newer: bool) -> set[str]:
    _setup(tmp_path, monkeypatch, legacy_is_newer=legacy_is_newer)

    import server
    from fastapi.testclient import TestClient

    resp = TestClient(server.app).get(f"/api/execution/{PLAN_ID}/progress")
    assert resp.status_code == 200, resp.text
    return {t["id"] for t in resp.json()["tasks"]}


def test_progress_prefers_newer_canonical_tasks_json(tmp_path, monkeypatch):
    """tasks.json newer than the legacy snapshot → the card follows it.

    The ghost ``R3-2`` (never runnable) must not appear, and the
    re-queued task list must be the one reported.
    """
    ids = _progress_task_ids(tmp_path, monkeypatch, legacy_is_newer=False)
    assert "R3-2" not in ids, (
        "a task that exists only in the stale legacy merged file must not "
        "show up in the progress card — it can never be run, so it pins "
        "the plan at 'pending' forever"
    )
    assert {"1", "2"} <= ids


def test_progress_falls_back_to_legacy_file_when_it_is_newer(
    tmp_path, monkeypatch
):
    """Backwards compat: pre-2026-09-08 plans keep working.

    When the legacy merged snapshot is the freshest record of the task
    list, it still wins — the mtime rule must not silently drop those
    plans' repair tasks.
    """
    ids = _progress_task_ids(tmp_path, monkeypatch, legacy_is_newer=True)
    assert "R3-2" in ids, (
        "the legacy merged file is the authoritative list for plans whose "
        "repair pipeline predates the single-writer refactor"
    )
