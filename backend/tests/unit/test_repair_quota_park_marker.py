"""2026-11-02: the repair path's quota-park marker reader.

Background
----------
When every provider runs out of capacity the executor parks the plan
instead of failing it: it writes ``stop_reason="waiting_for_quota"`` into
the top level of ``tasks.json`` and exits with rc=0.  The main execution
path already reads that marker through
``routes.execution._quota_wait_detail``; the repair path had no reader at
all (``grep waiting_for_quota backend/verification_loop.py`` matched
nothing), so a repair round that was parked for quota was indistinguishable
from a repair round that really had nothing left to do.

Contract pinned here
--------------------
* ``_repair_parked_for_quota`` is the repair path's only marker reader.
* The marker present → ``True``.
* No file / unreadable file / broken JSON / no ``stop_reason`` / a
  different ``stop_reason`` → ``False``, never an exception.
* A marker whose ``reason_detail`` is not JSON still counts: the detail
  is reported as an empty dict, but the marker itself stands.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` is imported first on purpose: it seeds ``sys.modules['server']``
# and then wires the ``routes`` package, and ``routes.execution`` late-binds
# back into it.
import server  # noqa: E402,F401
from verification_loop import _repair_parked_for_quota  # noqa: E402


def _write_tasks(directory: Path, payload: dict, name: str = "tasks.json") -> Path:
    tasks_file = directory / name
    tasks_file.write_text(json.dumps(payload), encoding="utf-8")
    return tasks_file


def test_marker_present_is_parked(tmp_path):
    tasks_file = _write_tasks(
        tmp_path,
        {
            "stop_reason": "waiting_for_quota",
            "reason_detail": "{}",
            "tasks": [{"id": "repair-r1-01"}],
        },
    )

    assert _repair_parked_for_quota(tasks_file) is True


def test_missing_or_other_reason_is_not_parked(tmp_path):
    # No file at all.
    assert _repair_parked_for_quota(tmp_path / "no-such-tasks.json") is False

    # A tasks.json without a stop_reason.
    no_reason = _write_tasks(tmp_path, {"tasks": []}, name="no-reason.json")
    assert _repair_parked_for_quota(no_reason) is False

    # A different stop_reason.
    other_reason = _write_tasks(
        tmp_path,
        {"stop_reason": "success", "reason_detail": "{}", "tasks": []},
        name="other-reason.json",
    )
    assert _repair_parked_for_quota(other_reason) is False

    # A tasks.json that is not JSON at all — a broken file is not a marker.
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert _repair_parked_for_quota(broken) is False


def test_broken_reason_detail_keeps_the_marker(tmp_path):
    tasks_file = _write_tasks(
        tmp_path,
        {
            "stop_reason": "waiting_for_quota",
            "reason_detail": "not-json-at-all",
            "tasks": [{"id": "repair-r1-01"}],
        },
    )

    assert _repair_parked_for_quota(tasks_file) is True
