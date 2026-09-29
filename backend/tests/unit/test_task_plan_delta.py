"""
Operator task-list delta: ``add`` / ``obsolete``, never modify.

Why this exists
-------------------------------------------
A failed execution task had only two outcomes: get repaired, or sit on
the progress card as a permanent red entry. That plan has four such
rows — create-tasks the generator could only mislabel read-only.
Clearing them would hide the problem rather than solve it. The
verification
side already had the right shape (``verification_plan_delta``: mark, do
not delete, never modify what exists); this is the execution-side
equivalent.

These tests pin the hard constraints and, critically, that applying an
``obsolete`` does not alter the task it voids.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)
from task_plan_delta import (  # noqa: E402
    OBSOLETE_REASON_PREFIX,
    apply_task_delta,
    is_obsolete_reason,
    load_delta_log,
    next_manual_task_id,
    obsolete_failure_reason,
    parse_task_delta_payload,
)


PLAN_ID = "test-plan-delta"


@pytest.fixture
def conn(tmp_path: Path):
    c = open_db(tmp_path / "state.db")
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _seed(conn, task_id, *, status="failed", title="t", description="d",
          test_command="pytest -q"):
    conn.execute(
        "INSERT INTO plan_tasks "
        "(plan_id, task_id, status, title, description, test_command, "
        " updated_at) VALUES (?,?,?,?,?,?, '2026-09-16T00:00:00Z')",
        (PLAN_ID, task_id, status, title, description, test_command),
    )
    conn.commit()


def _disk_tasks(tmp_path: Path, tasks):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8",
    )
    return plan_dir


# ---------------------------------------------------------------------------
# Hard constraints on ``add``
# ---------------------------------------------------------------------------


def test_add_without_title_is_rejected():
    d = parse_task_delta_payload(
        {"add": [{"reason": "r", "test_command": "pytest"}]}, set(),
    )
    assert d.added == []
    assert "缺少 title" in d.rejected_additions[0]["why"]


def test_add_without_reason_is_rejected():
    """'新增必须有依据' — same rule as the verification side."""
    d = parse_task_delta_payload(
        {"add": [{"title": "t", "test_command": "pytest"}]}, set(),
    )
    assert d.added == []
    assert "reason" in d.rejected_additions[0]["why"]


def test_add_without_test_command_is_rejected():
    """Stricter than the VP side, deliberately.

    An execution task's completion verdict cross-checks the subagent's
    claim against the command's exit code. Shipping a task with no
    command is not "adding a task", it is accruing a debt that will be
    judged on the model's word alone.
    """
    d = parse_task_delta_payload(
        {"add": [{"title": "t", "reason": "r"}]}, set(),
    )
    assert d.added == []
    assert "test_command" in d.rejected_additions[0]["why"]


def test_add_reusing_an_existing_id_is_a_modification():
    d = parse_task_delta_payload(
        {"add": [{"id": "11-5-1", "title": "t", "reason": "r",
                  "test_command": "pytest"}]},
        {"11-5-1"},
    )
    assert d.added == []
    assert d.rejected_modifications[0]["id"] == "11-5-1"
    assert "修改已有任务" in d.rejected_modifications[0]["why"]


def test_a_valid_add_is_accepted():
    d = parse_task_delta_payload(
        {"add": [{"title": "t", "reason": "r", "test_command": "pytest -q"}]},
        set(),
    )
    assert len(d.added) == 1
    assert d.rejected_additions == []


def test_non_dict_entries_are_skipped_not_crashed():
    d = parse_task_delta_payload({"add": ["nope", None, 3]}, set())
    assert d.added == []


def test_non_dict_payload_yields_an_empty_delta():
    d = parse_task_delta_payload(["not", "a", "dict"], set())
    assert d.is_empty


# ---------------------------------------------------------------------------
# Hard constraints on ``obsolete``
# ---------------------------------------------------------------------------


def test_obsolete_unknown_id_is_rejected():
    d = parse_task_delta_payload(
        {"obsolete": [{"id": "nope", "reason": "r"}]}, {"11-5-1"},
    )
    assert d.obsoleted == []
    assert "不存在" in d.rejected_obsoletes[0]["why"]


def test_obsolete_without_reason_is_rejected():
    d = parse_task_delta_payload(
        {"obsolete": [{"id": "11-5-1"}]}, {"11-5-1"},
    )
    assert d.obsoleted == []
    assert "reason" in d.rejected_obsoletes[0]["why"]


def test_obsolete_is_idempotent():
    """Voiding twice must not stack entries or re-record."""
    d = parse_task_delta_payload(
        {"obsolete": [{"id": "11-5-1", "reason": "r"}]},
        {"11-5-1"}, already_obsolete={"11-5-1"},
    )
    assert d.obsoleted == []
    assert d.rejected_obsoletes == []


def test_a_valid_obsolete_is_accepted():
    d = parse_task_delta_payload(
        {"obsolete": [{"id": "11-5-1", "reason": "空壳任务，无内容可执行"}]},
        {"11-5-1"},
    )
    assert d.obsoleted == [{"id": "11-5-1", "reason": "空壳任务，无内容可执行"}]


# ---------------------------------------------------------------------------
# Obsolete marker
# ---------------------------------------------------------------------------


def test_obsolete_reason_is_distinguishable_from_a_failure():
    text = obsolete_failure_reason("空壳任务", round_number=4)
    assert text.startswith(OBSOLETE_REASON_PREFIX)
    assert is_obsolete_reason(text) is True
    assert is_obsolete_reason("pytest exit 1") is False
    assert is_obsolete_reason(None) is False
    assert "round=4" in text


def test_obsolete_reason_survives_the_1000_char_truncation():
    """``record_task_failure`` truncates at 1000; the prefix must sit
    inside that budget, not past it."""
    text = obsolete_failure_reason("x" * 5000, round_number=1)
    assert len(text) <= 1000
    assert text.startswith(OBSOLETE_REASON_PREFIX)


# ---------------------------------------------------------------------------
# Id allocation
# ---------------------------------------------------------------------------


def test_next_manual_task_id_starts_at_one():
    assert next_manual_task_id(set(), 0) == "manual-01"


def test_next_manual_task_id_avoids_collisions():
    assert next_manual_task_id({"manual-01", "manual-02"}, 0) == "manual-03"
    assert next_manual_task_id({"manual-r4-01"}, 4) == "manual-r4-02"


def test_next_manual_task_id_ignores_non_numeric_tails():
    assert next_manual_task_id({"manual-abc", "manual-01"}, 0) == "manual-02"


def test_manual_ids_satisfy_the_repository_id_charset():
    """``_SAFE_TASK_ID_RE`` allows only ``[A-Za-z0-9_.:-]``."""
    from state_machine.repositories.plan_task_repository import (
        _SAFE_TASK_ID_RE,
    )

    assert _SAFE_TASK_ID_RE.fullmatch(next_manual_task_id(set(), 0))
    assert _SAFE_TASK_ID_RE.fullmatch(next_manual_task_id(set(), 12))


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _apply(tmp_path, conn, payload, disk_tasks):
    plan_dir = _disk_tasks(tmp_path, disk_tasks)
    existing_ids = {str(t.get("id")) for t in disk_tasks}
    existing_ids |= {str(t) for t in PlanTaskRepository(conn).load_all(PLAN_ID)}
    delta = parse_task_delta_payload(payload, existing_ids)
    summary = apply_task_delta(
        repo=PlanTaskRepository(conn),
        plan_id=PLAN_ID,
        plan_dir=plan_dir,
        existing_tasks=disk_tasks,
        delta=delta,
        round_number=4,
        actor="test",
    )
    return plan_dir, summary


def test_obsolete_marks_the_task_superseded(tmp_path, conn):
    _seed(conn, "repair-r4-01", status="failed", description="real content")
    _, summary = _apply(
        tmp_path, conn,
        {"obsolete": [{"id": "repair-r4-01", "reason": "已被 C3 修复取代"}]},
        [{"id": "disk-1", "title": "t", "description": "d",
          "test_command": "pytest"}],
    )
    assert summary["obsoleted"] == [
        {"id": "repair-r4-01", "reason": "已被 C3 修复取代"}
    ]

    row = PlanTaskRepository(conn).load_all(PLAN_ID)["repair-r4-01"]
    assert row["status"] == "superseded"
    assert is_obsolete_reason(row["failure_reason"])
    assert "已被 C3 修复取代" in row["failure_reason"]


def test_obsolete_does_not_touch_the_task_it_voids(tmp_path, conn):
    """The operator's core constraint: mark, never edit.

    ``description`` and ``test_command`` must be byte-identical after the
    void — they are the audit trail for why the task existed.
    """
    _seed(
        conn, "repair-r4-01",
        status="failed",
        title="恢复 select_entering_consolidation 行为冻结",
        description="895 字符的真实描述",
        test_command="cargo test --test signal_classification",
    )
    before = PlanTaskRepository(conn).load_all(PLAN_ID)["repair-r4-01"]

    _apply(
        tmp_path, conn,
        {"obsolete": [{"id": "repair-r4-01", "reason": "superseded"}]},
        [{"id": "disk-1", "title": "t", "description": "d",
          "test_command": "pytest"}],
    )
    after = PlanTaskRepository(conn).load_all(PLAN_ID)["repair-r4-01"]

    for field in ("title", "description", "test_command"):
        assert after.get(field) == before.get(field), (
            f"obsolete rewrote {field!r}; voiding must not modify the task"
        )


def test_add_writes_to_both_truth_sources(tmp_path, conn):
    """DB *and* tasks.json.

    DB-only would be lost on the next disk reload; disk-only would be
    invisible to the dispatcher's runtime overlay.
    """
    plan_dir, summary = _apply(
        tmp_path, conn,
        {"add": [{"title": "补一个替代任务", "reason": "替代已作废的空壳",
                  "description": "d", "test_command": "pytest tests/x.py -q"}]},
        [{"id": "disk-1", "title": "t", "description": "d",
          "test_command": "pytest"}],
    )
    assert len(summary["added"]) == 1
    new_id = summary["added"][0]["id"]

    assert new_id in PlanTaskRepository(conn).load_all(PLAN_ID)

    on_disk = json.loads(
        (plan_dir / "tasks.json").read_text(encoding="utf-8")
    )["tasks"]
    assert any(t["id"] == new_id for t in on_disk)
    assert len(on_disk) == 2, "the existing disk task must be preserved"


def test_add_keeps_the_declared_id(tmp_path, conn):
    _, summary = _apply(
        tmp_path, conn,
        {"add": [{"id": "manual-fix-1", "title": "t", "reason": "r",
                  "description": "d", "test_command": "pytest"}]},
        [],
    )
    assert summary["added"][0]["id"] == "manual-fix-1"


def test_add_colliding_within_the_batch_is_rejected(tmp_path, conn):
    """Two adds claiming the same id — the second must not overwrite."""
    _, summary = _apply(
        tmp_path, conn,
        {"add": [
            {"id": "dup", "title": "a", "reason": "r", "description": "d",
             "test_command": "pytest"},
            {"id": "dup", "title": "b", "reason": "r", "description": "d",
             "test_command": "pytest"},
        ]},
        [],
    )
    assert len(summary["added"]) == 1
    assert summary["rejected_modifications"]


def test_rejections_are_reported_in_the_summary(tmp_path, conn):
    _, summary = _apply(
        tmp_path, conn,
        {"add": [{"title": "no reason", "test_command": "pytest"}],
         "obsolete": [{"id": "ghost", "reason": "r"}]},
        [],
    )
    assert summary["rejected_additions"] == 1
    assert summary["rejected_obsoletes"] == 1


def test_delta_log_records_actor_reasons_and_rejections(tmp_path, conn):
    _seed(conn, "11-5-1", status="failed")
    plan_dir, _ = _apply(
        tmp_path, conn,
        {"obsolete": [{"id": "11-5-1", "reason": "空壳"}],
         "add": [{"title": "no reason"}]},
        [],
    )
    log = load_delta_log(plan_dir)
    assert len(log) == 1
    entry = log[0]
    assert entry["actor"] == "test"
    assert entry["round"] == 4
    assert entry["summary"]["obsoleted"][0]["reason"] == "空壳"
    assert entry["rejected"]["additions"]
    assert "ts" in entry


def test_both_actions_in_one_call(tmp_path, conn):
    _seed(conn, "repair-r4-03", status="failed")
    plan_dir, summary = _apply(
        tmp_path, conn,
        {"obsolete": [{"id": "repair-r4-03", "reason": "内容已由加载器恢复"}]},
        [],
    )
    assert len(summary["obsoleted"]) == 1
    assert summary["added"] == []
