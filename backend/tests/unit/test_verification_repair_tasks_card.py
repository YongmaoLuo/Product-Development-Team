"""TDD tests for the 2026-09-11 plan v12 (cards.py repair_tasks visibility).

Background (2026-09-11):
  When verification fails a VP, the orchestrator generates
  ``repair_tasks`` so a follow-up executor run can fix the failing
  VPs and re-verify. But the Feishu card had no way to surface:
    1. Which round we're currently on (1/N, 2/N, …)
    2. Whether the orchestrator generated repair_tasks at all
    3. The list of repair tasks (titles + test commands)

  Two coupled fixes:
    * :func:`_run_auto_verification_loop` writes
      ``plans/{id}/verification_repair_tasks.json`` after every round
      so the list survives server restart (was in-memory only).
    * :func:`_build_verification_progress` returns
      ``repair_tasks`` in the response (two-layer read: disk first,
      in-memory fallback).
    * :func:`_verification_sections` renders a round info line at the
      top of the verification card body.

  2026-09-18 update: the repair_tasks *section* it used to render was
  removed (repair tasks reach the operator through
  the pending-task list). The persistence + endpoint contracts below
  are unchanged; only the card rendering changed.

These tests pin all three contracts at the unit level.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from notifications.cards import _verification_sections

# ---------------------------------------------------------------------------
# Fix #1 — server.py persists repair_tasks; _build_verification_progress
# returns them; disk snapshot survives "restart" (in-memory cleared).
# ---------------------------------------------------------------------------


def _setup_plan_dir(plan_dir: Path, plan_id: str) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    # Minimal ``verification_plan.json`` so _build_verification_progress
    # doesn't 404. Schema is what the endpoint reads.
    (plan_dir / "verification_plan.json").write_text(
        json.dumps({"verification_points": [{"id": "VP-001", "title": "t"}]}),
        encoding="utf-8",
    )


def _write_disk_repair_tasks(plan_dir: Path, plan_id: str, tasks: list) -> Path:
    """Write the persistence file the v12 fix creates."""
    p = plan_dir / "verification_repair_tasks.json"
    p.write_text(
        json.dumps({
            "plan_id": plan_id,
            "round": 2,
            "generated_at": "2026-09-11T15:00:00Z",
            "tasks": tasks,
        }),
        encoding="utf-8",
    )
    return p


def test_build_verification_progress_includes_disk_repair_tasks(tmp_path, monkeypatch):
    """Disk snapshot is returned in the response payload (preferred over in-memory)."""
    plan_id = "test-plan-rp-disk"
    plan_dir = tmp_path / "plans" / plan_id
    _setup_plan_dir(plan_dir, plan_id)
    _write_disk_repair_tasks(plan_dir, plan_id, [
        {"id": "RP-1", "title": "修复 VP-006 函数体 diff", "test_command": "cargo test"}
    ])

    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    # Seed the plan_verification SQLite row so the endpoint doesn't 404.
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    conn = open_db(str(tmp_path / "state.db"))
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.insert(plan_id, "running", round=1, max_rounds=3)
        repo.update_progress_state(
            plan_id,
            completed_vps=[],
            failed_vps=[],
            skipped_vps=[],
            current_vp=None,
        )
    finally:
        conn.close()

    # Force in-memory state to be EMPTY to prove disk read wins.
    from server import _verification_state
    _verification_state.pop(plan_id, None)

    from server import _build_verification_progress
    progress = _build_verification_progress(plan_id)
    assert "repair_tasks" in progress, "response must include repair_tasks"
    rt = progress["repair_tasks"]
    assert len(rt) == 1
    assert rt[0]["id"] == "RP-1"
    assert rt[0]["title"] == "修复 VP-006 函数体 diff"


def test_repair_tasks_endpoint_reads_disk_snapshot(tmp_path, monkeypatch):
    """GET /api/verification/{id}/repair_tasks reads disk after 'restart'."""
    plan_id = "test-plan-rp-endpoint"
    plan_dir = tmp_path / "plans" / plan_id
    _setup_plan_dir(plan_dir, plan_id)
    _write_disk_repair_tasks(plan_dir, plan_id, [
        {"id": "RP-A", "title": "fix A"},
        {"id": "RP-B", "title": "fix B", "test_command": "pytest x.py"},
    ])

    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    from server import _verification_state
    _verification_state.pop(plan_id, None)

    from server import app
    client = TestClient(app)
    r = client.get(f"/api/verification/{plan_id}/repair_tasks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["tasks"]) == 2
    assert {t["id"] for t in body["tasks"]} == {"RP-A", "RP-B"}


def test_repair_tasks_dedup_disk_and_memory(tmp_path, monkeypatch):
    """When both disk and in-memory have the same id, disk wins (seen first)."""
    plan_id = "test-plan-rp-dedup"
    plan_dir = tmp_path / "plans" / plan_id
    _setup_plan_dir(plan_dir, plan_id)
    _write_disk_repair_tasks(plan_dir, plan_id, [
        {"id": "RP-1", "title": "disk version", "test_command": "from-disk"},
    ])

    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    from server import _verification_state
    _verification_state[plan_id] = {
        "verification_status": "running",
        "verification_round": 3,
        "repair_tasks": [
            {"id": "RP-1", "title": "memory version", "test_command": "from-mem"},
            {"id": "RP-2", "title": "memory only"},
        ],
    }

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    conn = open_db(str(tmp_path / "state.db"))
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.insert(plan_id, "running", round=3, max_rounds=3)
        repo.update_progress_state(
            plan_id,
            completed_vps=[],
            failed_vps=[],
            skipped_vps=[],
            current_vp=None,
        )
    finally:
        conn.close()

    from server import _build_verification_progress
    progress = _build_verification_progress(plan_id)
    ids = [t["id"] for t in progress["repair_tasks"]]
    # Disk version of RP-1 wins; RP-2 from memory is appended (no duplicate).
    assert ids == ["RP-1", "RP-2"], f"unexpected order/dedup: {ids!r}"
    assert progress["repair_tasks"][0]["title"] == "disk version"


# ---------------------------------------------------------------------------
# Fix #2 — cards.py _verification_sections renders round + repair_tasks
# ---------------------------------------------------------------------------


def test_verification_sections_shows_round_info_when_running():
    """Round 2/3 line appears at the top when status=running + round set."""
    progress = {
        "verification_status": "running",
        "verification_round": 2,
        "max_rounds": 3,
        "vps": [],
        "completed_vps": [],
        "failed_vps": [],
        "skipped_vps": [],
    }
    elements = _verification_sections(progress, is_terminal=False)
    # First non-empty div should mention round 2/3.
    found_round = False
    for e in elements:
        if isinstance(e, dict) and e.get("tag") == "div":
            text = e.get("text", {}).get("content", "")
            if "round 2/3" in text and "当前轮次" in text:
                found_round = True
                break
    assert found_round, (
        f"expected round 2/3 line in card body, got elements: {elements!r}"
    )


def test_verification_sections_does_not_render_repair_tasks_section():
    """No "🔧 反思生成修复任务" section, even when repair_tasks is non-empty.

    2026-09-18: "生成的修复任务不需要单独的列一个章节
    出来，因为现在它已经能够正确的被放到待执行的任务列表里面". A repair
    task is appended to ``plan_tasks`` at generation time, so it reaches
    the operator through the execution area's "📋 等待中的任务" section.
    Rendering it again here only lengthened the card.
    """
    progress = {
        "verification_status": "running",
        "verification_round": 2,
        "max_rounds": 3,
        "repair_tasks": [
            {
                "id": "RP-1",
                "title": "修复 VP-006 函数体 diff",
                "test_command": "cargo test --test divergence",
                "failure_reason": "函数体非空 diff",
            },
            {
                "id": "RP-2",
                "title": "添加 metric 区间不可变测试",
                "test_command": "pytest tests/test_metric_invariants.py",
            },
        ],
        "vps": [],
        "completed_vps": [],
        "failed_vps": [],
        "skipped_vps": [],
    }
    elements = _verification_sections(progress, is_terminal=False)
    for e in elements:
        if not isinstance(e, dict) or e.get("tag") != "div":
            continue
        text = e.get("text", {}).get("content", "")
        assert "反思生成修复任务" not in text, (
            f"repair tasks must not get a dedicated section, got: {text!r}"
        )
        assert "cargo test --test divergence" not in text, (
            f"repair task commands belong to the pending-task list, "
            f"not the verification card, got: {text!r}"
        )
    # The round line — the part of this section's contract that stays —
    # is still rendered.
    assert any(
        "round 2/3" in (e.get("text", {}).get("content", ""))
        for e in elements
        if isinstance(e, dict) and e.get("tag") == "div"
    ), f"round line must survive, got: {elements!r}"


def test_repair_tasks_payload_still_reaches_the_card_progress():
    """The data is untouched — only the duplicate rendering was removed.

    ``repair_tasks`` still flows into ``_verification_sections`` (used by
    the stuck-state guidance block) and is still returned by the
    progress endpoint. Removing the section must not break either.
    """
    progress = {
        "verification_status": "loop_stopped",
        "verification_round": 3,
        "max_rounds": 3,
        "repair_tasks": [{"id": "RP-9", "title": "补 x"}],
        "failed_vps": ["VP-001"],
        "vps": [{"id": "VP-001", "title": "t"}],
    }
    # The guidance block is gated on state_dict, not on the progress
    # payload — supply a terminal max-rounds state to switch it on.
    state = {
        "current_phase": "verification_loop_stopped",
        "verification": {
            "status": "loop_stopped",
            "stop_reason": "max_rounds_reached",
            "round": 3,
            "max_rounds": 3,
        },
    }
    from plan_status import PlanStatus

    elements = _verification_sections(
        progress, is_terminal=True,
        status=PlanStatus(
            plan_id="plan-x",
            phase=state.get("current_phase") or "",
            verification_status=state["verification"]["status"],
            verification_round=state["verification"]["round"],
            verification_max_rounds=state["verification"]["max_rounds"],
            verification_stop_reason=state["verification"]["stop_reason"],
        ),
    )
    joined = "\n".join(
        e.get("text", {}).get("content", "")
        for e in elements
        if isinstance(e, dict) and e.get("tag") == "div"
    )
    assert "RP-9" in joined, (
        f"stuck-state guidance should still name the queued repair ids, "
        f"got: {joined!r}"
    )


def test_verification_sections_no_round_when_terminal():
    """Terminal plans (passed/failed) skip the round info line."""
    progress = {
        "verification_status": "passed",
        "verification_round": 3,
        "max_rounds": 3,
        "vps": [],
        "completed_vps": ["VP-001"],
        "failed_vps": [],
        "skipped_vps": [],
    }
    elements = _verification_sections(progress, is_terminal=True)
    for e in elements:
        if isinstance(e, dict) and e.get("tag") == "div":
            text = e.get("text", {}).get("content", "")
            assert "当前轮次" not in text, (
                f"terminal card should not show '当前轮次', got: {text!r}"
            )


def test_verification_sections_empty_repair_tasks_no_section():
    """When repair_tasks=[] (no failed VPs → no repairs), no section rendered."""
    progress = {
        "verification_status": "running",
        "verification_round": 1,
        "max_rounds": 3,
        "repair_tasks": [],
        "vps": [],
        "completed_vps": ["VP-001"],
        "failed_vps": [],
        "skipped_vps": [],
    }
    elements = _verification_sections(progress, is_terminal=False)
    for e in elements:
        if isinstance(e, dict) and e.get("tag") == "div":
            text = e.get("text", {}).get("content", "")
            assert "反思生成修复任务" not in text, (
                f"empty repair_tasks must not render a section, got: {text!r}"
            )