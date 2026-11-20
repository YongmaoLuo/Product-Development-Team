"""A plan's ``mode`` lives in ``plan_state.flags`` and outlives the process.

The mode is the difference between "exercise the pipeline" and "run it for
real", so it cannot live in a process-local variable: the cost gate, the
executor and the verification sub-agent are three different processes that
have to agree on it.  It lives in the plan's routing row instead, under
``flags["mode"]``, and every reader gets it from there.

The properties pinned here:

* a plan that says nothing runs for real -- ``ensure_row`` gives a fresh
  plan ``production``;
* what one writer sets, another reader sees -- ``set_mode("test")`` is
  visible to a *different* ``PlanState`` instance;
* the module-level reader agrees with the class method;
* a mode nobody recognises is not an error -- it falls back to
  ``production``, says so in the log, and the fallback is what gets
  stored.

Every test points ``PDT_STATE_DB_PATH`` at its own temporary database
first, so nothing here can touch the operator's real state store.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _plan_dir(tmp_path: Path) -> Path:
    """An empty plan directory -- no artifacts, so no branch conflict."""
    plan_dir = tmp_path / "plan-mode-flag"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


def _routing_flags(plan_id: str):
    """The persisted ``flags`` of a plan's routing row -- or ``None``.

    ``None`` means "there is no row", which is not the same as "the row
    has empty flags": the table itself may not exist yet, because the
    routing row is created by the first real write.
    """
    db = Path(os.environ["PDT_STATE_DB_PATH"])
    with sqlite3.connect(str(db)) as conn:
        try:
            row = conn.execute(
                "SELECT flags FROM plan_routing WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # no routing table yet -- nothing was written
    return json.loads(row[0]) if row and row[0] else None


def test_default_state_mode_is_production(tmp_path, monkeypatch):
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    from plan_state import PlanState

    plan_dir = _plan_dir(tmp_path)
    ps = PlanState(plan_dir)
    ps.ensure_row()

    assert ps.get_mode() == "production"


def test_set_mode_persists_across_instances(tmp_path, monkeypatch):
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    from plan_state import PlanState

    plan_dir = _plan_dir(tmp_path)
    ps = PlanState(plan_dir)
    ps.ensure_row()

    assert ps.set_mode("test") == "test"

    # A *different* instance reads it back from the persisted row.
    assert PlanState(plan_dir).get_mode() == "test"


def test_read_plan_mode_reads_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    from plan_state import PlanState, read_plan_mode

    plan_dir = _plan_dir(tmp_path)
    ps = PlanState(plan_dir)
    ps.ensure_row()
    ps.set_mode("test")

    assert read_plan_mode(plan_dir) == "test"


def test_set_mode_invalid_falls_back(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    from plan_state import PlanState

    plan_dir = _plan_dir(tmp_path)
    ps = PlanState(plan_dir)
    ps.ensure_row()

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        resolved = ps.set_mode("prod")

    assert resolved == "production"
    assert "plan_mode_invalid=" in caplog.text


def test_reading_the_mode_leaves_no_row_and_setting_it_lands_one(
    tmp_path, monkeypatch
):
    """Reading a mode must not create a plan; setting one must.

    Constructing a ``PlanState`` is a pure read (2026-11-11) -- the
    routing row appears on the first real write.  So a caller that only
    wants to know a plan's mode must not leave a row behind for a plan
    that has none, and ``set_mode`` -- being a write -- must leave one
    behind, carrying the mode it stored.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    from plan_state import PlanState

    plan_dir = _plan_dir(tmp_path)

    PlanState(plan_dir)
    assert _routing_flags(plan_dir.name) is None

    ps = PlanState(plan_dir)
    assert ps.set_mode("test") == "test"

    flags = _routing_flags(plan_dir.name)
    assert flags is not None
    assert flags["mode"] == "test"
