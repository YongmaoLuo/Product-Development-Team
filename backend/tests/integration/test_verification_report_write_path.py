"""The report's write path: one writer, two formats, no half-written files.

What this pins
--------------
``verification_report.json`` is the only place a verification conclusion
lives; ``verification-report.md`` is the view of it that people read.
Both come out of **one** write path — the persistence manager's
``update_report`` — because two writers means two truths: the JSON says
one thing, the Markdown another, and the reader has no way to tell which
one is current.

Four properties, each one a way to lose a report:

  1. **Both files exist after a write.**  The Markdown is not written by
     some other component, later, "when somebody asks for it" — it is
     written by the same call that writes the JSON.
  2. **A rendering failure keeps the JSON.**  The Markdown is a derived
     view; a broken view must not take the data with it.  The JSON on
     disk is byte-for-byte what it was before the failed write.
  3. **No half-written Markdown.**  The file at the final path is either
     the previous version or the complete new one — never a truncated
     one, and never a leftover temp file.
  4. **The report carries its extension fields.**  ``plan_item_id``,
     ``skipped_items``, ``confirmed_defects``, ``repair_records``,
     ``unresolved``, ``human_decisions`` and ``final_verdict`` are what
     the readers key on; a report missing one of them is a report a
     reader cannot read.

And one join, because a repair that cannot be traced to a commit is a
repair nobody can audit: ``repair_records`` carries the commit the
repair landed in, joined from the plan task.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_persistence  # noqa: E402
from verification_persistence import (  # noqa: E402
    REPORT_EXTENSION_FIELDS,
    REPORT_MD_FILENAME,
    VerificationPersistenceManager,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """A plan directory — the manager's whole world."""
    path = tmp_path / "plan-2026-10-25"
    path.mkdir()
    return path


@pytest.fixture
def manager(plan_dir: Path) -> VerificationPersistenceManager:
    """The persistence manager, pointed at the plan directory."""
    return VerificationPersistenceManager(plan_dir)


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _read_report(plan_dir: Path) -> Dict[str, Any]:
    """The report as it sits on disk."""
    return json.loads(
        (plan_dir / "verification_report.json").read_text(encoding="utf-8")
    )


def _readable_field_names(report: Dict[str, Any]) -> set:
    """Every field name a reader can find in the report.

    The report's fields do not all live at the same level: the
    verification-point fields belong to the points, the rest to the
    report.  "Readable" means reachable, not "at the top level".
    """
    names = set(report.keys())
    for field in ("verification_points", "verification_results"):
        points = report.get(field)
        if isinstance(points, list):
            for point in points:
                if isinstance(point, dict):
                    names.update(point.keys())
    return names


def _leftover_temp_files(plan_dir: Path) -> List[str]:
    """The staging files the write path forgot to clean up."""
    return [
        name
        for name in os.listdir(plan_dir)
        if name.endswith(".tmp")
    ]


def _persist_plan_task(plan_id: str, task: Dict[str, Any], commit_sha: str) -> None:
    """Write one plan task — and its commit — the way the repair round does.

    The repair round persists its tasks to ``state.db``'s ``plan_tasks``
    table (never to ``tasks.json``), and the commit the fix landed in is
    written back afterwards as a runtime field.  That is the pair the
    report's ``repair_records`` joins.
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        repo = PlanTaskRepository(conn)
        repo.add_task(plan_id, dict(task))
        repo.update_task(plan_id, task["id"], {"commit_sha": commit_sha})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. One write, two files.
# ---------------------------------------------------------------------------


def test_write_renders_markdown(manager, plan_dir):
    """The write produces the JSON and its Markdown view, together.

    The Markdown is not a separate step somebody else owns: it is the
    view of the report, written by the same call that writes the report.
    A reader who opens the plan directory after a verification round
    finds both, and the Markdown says what the JSON says.
    """
    manager.update_report(
        {
            "final_verdict": "FAILED",
            "verification_points": [
                {"id": "VP-001", "plan_item_id": "ITEM-1", "status": "failed"},
            ],
        }
    )

    report_file = plan_dir / "verification_report.json"
    md_file = plan_dir / REPORT_MD_FILENAME
    assert report_file.exists(), f"the report was not written to {report_file}"
    assert md_file.exists(), f"the Markdown view was not written to {md_file}"

    report = _read_report(plan_dir)
    assert report["final_verdict"] == "FAILED"

    markdown = md_file.read_text(encoding="utf-8")
    assert "VP-001" in markdown, (
        f"the Markdown must show the verification points; got {markdown!r}"
    )
    assert "FAILED" in markdown, (
        f"the Markdown must show the verdict; got {markdown!r}"
    )


# ---------------------------------------------------------------------------
# 2. A broken renderer does not take the report with it.
# ---------------------------------------------------------------------------


def test_render_failure_keeps_json(monkeypatch, manager, plan_dir):
    """The renderer blows up → the JSON on disk is untouched.

    The Markdown is a derived view; the JSON is the data.  A renderer
    that raises says something about *this* report, not about the
    previous one — so the previous report stays exactly as it was,
    rather than being replaced by a report the renderer refuses to
    render.

    The failure is not swallowed: the caller has to learn that the write
    did not happen, or it will go on believing the new report is on
    disk.
    """
    manager.update_report(
        {
            "final_verdict": "PASSED",
            "verification_points": [
                {"id": "VP-001", "plan_item_id": "ITEM-1", "status": "passed"},
            ],
        }
    )

    report_file = plan_dir / "verification_report.json"
    md_file = plan_dir / REPORT_MD_FILENAME
    report_before = report_file.read_bytes()
    markdown_before = md_file.read_bytes()

    def _explode(*args, **kwargs):
        raise RuntimeError("the renderer exploded")

    monkeypatch.setattr(verification_persistence, "render_markdown", _explode)

    with pytest.raises(RuntimeError):
        manager.update_report(
            {
                "final_verdict": "FAILED",
                "verification_points": [
                    {"id": "VP-001", "plan_item_id": "ITEM-1", "status": "failed"},
                ],
            }
        )

    assert report_file.read_bytes() == report_before, (
        "the failed write must leave the previous report exactly as it was"
    )
    assert md_file.read_bytes() == markdown_before, (
        "the failed write must leave the previous Markdown exactly as it was"
    )
    assert _leftover_temp_files(plan_dir) == [], (
        "the failed write left staging files behind"
    )


# ---------------------------------------------------------------------------
# 3. No half-written Markdown.
# ---------------------------------------------------------------------------


def test_atomic_write_no_partial_md(monkeypatch, manager, plan_dir):
    """The disk refuses the Markdown → there is no Markdown, not half of one.

    A truncated Markdown is worse than none: it reads like a report
    whose last section simply ends mid-table, and a reader cannot tell
    it apart from a report that was cut short on purpose.  So the file
    at the final path is only ever the previous version or the complete
    new one.

    The JSON is not collateral damage: it is written before the
    Markdown, so it is on disk and complete even when the Markdown
    could not be stored.
    """
    real_replace = os.replace

    def _exploding_replace(src, dst, *args, **kwargs):
        if str(dst).endswith(REPORT_MD_FILENAME):
            raise OSError("no space left on device")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(verification_persistence.os, "replace", _exploding_replace)

    with pytest.raises(OSError):
        manager.update_report(
            {
                "final_verdict": "PASSED",
                "verification_points": [
                    {"id": "VP-001", "plan_item_id": "ITEM-1", "status": "passed"},
                ],
            }
        )

    md_file = plan_dir / REPORT_MD_FILENAME
    assert not md_file.exists(), (
        "a Markdown that could not be written must not be left half-written"
    )
    assert _leftover_temp_files(plan_dir) == [], (
        "the failed write left staging files behind"
    )

    report = _read_report(plan_dir)
    assert report["final_verdict"] == "PASSED", (
        "the JSON must not depend on the Markdown being writable"
    )


# ---------------------------------------------------------------------------
# 4. The report carries its extension fields.
# ---------------------------------------------------------------------------


def test_report_extension_fields_present(manager, plan_dir):
    """Every field the readers key on is in the report.

    The report is written by an LLM round trip (and by the fallback
    path, and by hand), so it may arrive with any of these missing.
    The write boundary is the one place that knows what the report is
    supposed to contain, so it is the one place that fills the gaps in.
    """
    manager.update_report(
        {
            "final_verdict": "FAILED",
            "verification_points": [
                {"id": "VP-001", "plan_item_id": "ITEM-1", "status": "failed"},
                {"id": "VP-002", "status": "skipped"},
            ],
            "skipped_items": ["VP-002"],
            "confirmed_defects": [
                {"vp_id": "VP-001", "repair_task_id": "repair-r1-vp-001"},
            ],
        }
    )

    report = _read_report(plan_dir)
    readable = _readable_field_names(report)
    missing = [name for name in REPORT_EXTENSION_FIELDS if name not in readable]
    assert not missing, (
        f"the report does not carry {missing}; it has {sorted(readable)!r}"
    )

    assert report["skipped_items"] == ["VP-002"]
    assert report["confirmed_defects"][0]["vp_id"] == "VP-001"
    assert report["confirmed_defects"][0]["repair_task_id"] == "repair-r1-vp-001"
    assert report["final_verdict"] == "FAILED"
    assert report["unresolved"] == []
    assert report["human_decisions"] == []
    assert isinstance(report["repair_records"], list)

    # ``plan_item_id`` belongs to the verification point: it says which
    # plan-document entry the point traces back to.  A point that does
    # not carry one falls back to its own id.
    points = {point["id"]: point for point in report["verification_points"]}
    assert points["VP-001"]["plan_item_id"] == "ITEM-1"
    assert points["VP-002"]["plan_item_id"] == "VP-002"


# ---------------------------------------------------------------------------
# 5. The repair record carries the commit the repair landed in.
# ---------------------------------------------------------------------------


def test_repair_records_join_plan_task_commit(manager, plan_dir):
    """A confirmed defect is joined with the commit its repair landed in.

    The defect is in the report; the commit is on the plan task.  The
    join key is the verification point.  Without it the report says
    "this was repaired" and stops there — and nobody can check whether
    the repair is the one that is on disk.
    """
    _persist_plan_task(
        plan_dir.name,
        {
            "id": "repair-r1-vp-001",
            "title": "修复 VP-001",
            "failed_vp_id": "VP-001",
        },
        commit_sha="abc123def456",
    )

    manager.update_report(
        {
            "final_verdict": "FAILED",
            "confirmed_defects": [
                {"vp_id": "VP-001", "repair_task_id": "repair-r1-vp-001"},
            ],
        }
    )

    report = _read_report(plan_dir)
    records = report["repair_records"]
    assert len(records) == 1, f"expected one repair record; got {records!r}"
    assert records[0]["vp_id"] == "VP-001"
    assert records[0]["repair_task_id"] == "repair-r1-vp-001"
    assert records[0]["commit_sha"] == "abc123def456"
