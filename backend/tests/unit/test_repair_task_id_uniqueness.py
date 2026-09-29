"""Repair task ids are never reused within a plan.

2026-09-20 (post-mortem)
-----------------------------

``RepairTaskAssembler`` stamped ``repair-r{N}-{seq}`` with ``seq`` always
starting at ``1``. That makes the id a function of ``(round, position in
batch)`` — and a **counter reset** hands the plan a fresh round 1. Every
batch for the same round therefore produced the same ids.

``plan_tasks`` is keyed on ``(plan_id, task_id)``, so the second batch
did not duplicate the first; it **overwrote** it, taking the row's
``attempt`` / ``failure_reason`` / ``commit_sha`` with it. Three
batches in one run all numbered from ``repair-r1-01``::

    repair-r1-06-1   completed   (batch 2's child)
    repair-r1-06-2   completed
    repair-r1-06-3   completed
    repair-r1-06     completed   (batch 3's parent)

The children — still present in ``tasks.json`` — now hang off a task
written later that repairs a different verification point.

``next_repair_seq_base`` fixes this by making ``seq`` an allocation
counter for ``(plan, round)`` that only ever increases, so an id is
never reissued. ``cross_store_task_conflicts`` is the check that would
have made the split visible: it compares ``tasks.json`` against
``plan_tasks`` and reports ids the two stores disagree about.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from repair_generator import (
    RepairTaskAssembler,
    cross_store_task_conflicts,
    next_repair_seq_base,
    parse_repair_task_id,
)
from verification import failure_history as fh


def _vp(vp_id: str, priority: str = "medium") -> dict:
    return {
        "id": vp_id,
        "title": f"VP {vp_id} title",
        "priority": priority,
        "actual_result": "broken",
        "evidence": "...",
    }


def _contents(*vp_ids: str) -> list:
    return [
        {"failed_vp_id": vp_id, "title": f"Fix {vp_id}", "description": "d"}
        for vp_id in vp_ids
    ]


# ---------------------------------------------------------------------------
# parse_repair_task_id
# ---------------------------------------------------------------------------

class TestParseRepairTaskId:
    def test_plain_id(self):
        assert parse_repair_task_id("repair-r1-06") == (1, 6)

    def test_breakdown_child_resolves_to_its_batch_slot(self):
        """``repair-r1-06-1`` is a breakdown of batch slot 06, not a new slot.

        Missing this would let a child id inflate the base past ids that
        are still free — and, worse, would let the *parent* slot be
        reissued while children reference it.
        """
        assert parse_repair_task_id("repair-r1-06-1") == (1, 6)
        assert parse_repair_task_id("repair-r3-03-1-1-2") == (3, 3)

    def test_double_digit_round_and_seq(self):
        assert parse_repair_task_id("repair-r12-07") == (12, 7)

    def test_non_repair_ids_are_none(self):
        for task_id in ("1-1", "11-5-1-1", "RP-004", "", None, "repair-rX-01"):
            assert parse_repair_task_id(task_id) is None, (
                f"{task_id!r} parsed as a repair id — it would then inflate "
                f"the allocation base for a round it never belonged to"
            )


# ---------------------------------------------------------------------------
# next_repair_seq_base
# ---------------------------------------------------------------------------

class TestNextRepairSeqBase:
    def test_empty_history_starts_at_one(self):
        assert next_repair_seq_base([], 1) == 1

    def test_continues_after_the_highest_seq_of_this_round(self):
        ids = ["repair-r1-01", "repair-r1-02", "repair-r1-03"]
        assert next_repair_seq_base(ids, 1) == 4

    def test_other_rounds_do_not_shift_the_base(self):
        ids = ["repair-r2-01", "repair-r2-99"]
        assert next_repair_seq_base(ids, 1) == 1

    def test_breakdown_children_count(self):
        assert next_repair_seq_base(["repair-r1-06-1"], 1) == 7

    def test_original_stream_ids_are_ignored(self):
        ids = ["1-1", "11-5-1-1", "RP-004", "repair-rX-01"]
        assert next_repair_seq_base(ids, 1) == 1

    def test_none_is_tolerated(self):
        assert next_repair_seq_base(None, 1) == 1


# ---------------------------------------------------------------------------
# cross_store_task_conflicts
# ---------------------------------------------------------------------------

class TestCrossStoreTaskConflicts:
    def test_agreeing_stores_report_nothing(self):
        titles = {"repair-r1-01": "修复 VP-001"}
        assert cross_store_task_conflicts(titles, dict(titles)) == {}

    def test_same_id_different_title_is_reported(self):
        """The observed shape, verbatim: one id, two unrelated VPs.

        ``tasks.json`` still holds the 2026-09-18 batch's round-1 task
        under slot 01 while ``plan_tasks`` holds the 2026-09-20 batch's —
        they repair different verification points entirely.
        """
        disk = {"repair-r1-01": "修复 VP-013:恢复盘整分支降序选择"}
        db = {"repair-r1-01": "修复 VP-001：让 9/3 14:09 盘整信号由 Rust 链路 emit"}
        assert cross_store_task_conflicts(disk, db) == {
            "repair-r1-01": (
                "修复 VP-013:恢复盘整分支降序选择",
                "修复 VP-001：让 9/3 14:09 盘整信号由 Rust 链路 emit",
            )
        }

    def test_id_present_in_only_one_store_is_not_a_conflict(self):
        """An id only one store knows about is the normal reconcile gap."""
        assert cross_store_task_conflicts({"a": "t"}, {"b": "t"}) == {}

    def test_whitespace_differences_are_not_conflicts(self):
        assert cross_store_task_conflicts({"a": " t "}, {"a": "t"}) == {}

    def test_fullwidth_punctuation_is_not_a_conflict(self):
        """``tasks.json`` keeps the LLM's ASCII colon; ``plan_tasks`` does not.

        Six of the thirteen raw repair-id disagreements were this
        and nothing else — e.g. ``修复 VP-001:清除 api_routes…``
        against ``修复 VP-001：清除 api_routes…``. Flagging them would
        bury the seven that describe genuinely different tasks.
        """
        disk = {"repair-r3-01": "修复 VP-001:清除硬编码(按 symbol/日期)"}
        db = {"repair-r3-01": "修复 VP-001：清除硬编码（按 symbol/日期）"}
        assert cross_store_task_conflicts(disk, db) == {}

    def test_a_blank_title_on_one_side_is_not_a_conflict(self):
        """Original-stream rows carry ``title=NULL`` in ``plan_tasks``.

        They all carry it — the column predates being populated.
        """
        assert cross_store_task_conflicts({"1-1": "真的标题"}, {"1-1": ""}) == {}
        assert cross_store_task_conflicts({"1-1": ""}, {"1-1": "别的"}) == {}

    def test_none_arguments_are_tolerated(self):
        assert cross_store_task_conflicts(None, None) == {}


# ---------------------------------------------------------------------------
# RepairTaskAssembler.seq_base
# ---------------------------------------------------------------------------

class TestAssemblerSeqBase:
    def test_default_base_reproduces_the_original_numbering(self):
        tasks = RepairTaskAssembler(
            round_number=1, failed_vps=[_vp("VP-1"), _vp("VP-2")],
        ).assemble(_contents("VP-1", "VP-2"))
        assert [t["id"] for t in tasks] == ["repair-r1-01", "repair-r1-02"]

    def test_base_shifts_the_whole_batch(self):
        tasks = RepairTaskAssembler(
            round_number=1, failed_vps=[_vp("VP-1"), _vp("VP-2")],
            seq_base=19,
        ).assemble(_contents("VP-1", "VP-2"))
        assert [t["id"] for t in tasks] == ["repair-r1-19", "repair-r1-20"]

    def test_first_task_of_a_shifted_batch_has_no_dependency(self):
        """The predecessor id belongs to an earlier batch.

        Linking to it would make this task wait on an unrelated repair
        from days ago — which the DAG validator would accept (the id
        exists) and the executor would honour.
        """
        tasks = RepairTaskAssembler(
            round_number=1, failed_vps=[_vp("VP-1")], seq_base=19,
        ).assemble(_contents("VP-1"))
        assert tasks[0]["depends_on"] == []

    def test_second_task_of_a_shifted_batch_chains_within_the_batch(self):
        tasks = RepairTaskAssembler(
            round_number=1, failed_vps=[_vp("VP-1"), _vp("VP-2")],
            seq_base=19,
        ).assemble(_contents("VP-1", "VP-2"))
        assert tasks[1]["depends_on"] == ["repair-r1-19"]

    def test_base_below_one_is_rejected(self):
        with pytest.raises(ValueError):
            RepairTaskAssembler(
                round_number=1, failed_vps=[_vp("VP-1")], seq_base=0,
            )


# ---------------------------------------------------------------------------
# The regression itself: two batches, one round, no shared ids
# ---------------------------------------------------------------------------

def test_a_second_batch_never_reissues_an_id():
    """Replaying the failure: reset the counter, re-run round 1.

    Before the fix the second batch emitted ``repair-r1-01`` … verbatim,
    and ``plan_tasks`` (keyed on ``(plan_id, task_id)``) overwrote the
    first batch's rows. Here the first batch's ids — including a
    breakdown child — are fed back in as the existing allocation, and
    the second batch must clear all of them.
    """
    first = RepairTaskAssembler(
        round_number=1, failed_vps=[_vp(f"VP-{i}") for i in range(1, 4)],
    ).assemble(_contents("VP-1", "VP-2", "VP-3"))
    first_ids = {t["id"] for t in first}
    # A breakdown of the third task exists by the time the plan is reset.
    existing = first_ids | {"repair-r1-03-1", "repair-r1-03-2"}

    second = RepairTaskAssembler(
        round_number=1,
        failed_vps=[_vp("VP-9")],
        seq_base=next_repair_seq_base(existing, 1),
    ).assemble(_contents("VP-9"))

    assert set(t["id"] for t in second).isdisjoint(existing), (
        "the second batch reused an id from the first — plan_tasks would "
        "overwrite that row and orphan its breakdown children"
    )
    # And the second batch's own dependency chain still resolves.
    emitted = {t["id"] for t in second}
    for task in second:
        for dep in task["depends_on"]:
            assert dep in emitted


def test_a_second_batch_for_a_different_round_is_unaffected():
    """Round 2 keeps its own numbering — only the *same* round collides."""
    existing = {"repair-r1-01", "repair-r1-02"}
    assert next_repair_seq_base(existing, 2) == 1


# ---------------------------------------------------------------------------
# State-store readers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> Path:
    """Minimal state.db whose ``plan_tasks`` matches the real schema."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE plan_tasks (
            plan_id TEXT, task_id TEXT, status TEXT, end_ts TEXT,
            schedule_ts TEXT, attempt INTEGER, commit_sha TEXT,
            failure_reason TEXT, breakdown_count INTEGER,
            _repo_version INTEGER, title TEXT, description TEXT,
            test_command TEXT, files_to_modify TEXT, depends_on TEXT,
            model_type TEXT, project_dir TEXT, provider TEXT,
            task_group TEXT, execution_group INTEGER, priority TEXT,
            acceptance_criteria TEXT, failed_vp_id TEXT, round INTEGER,
            updated_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return db


def _insert(db: Path, **row) -> None:
    conn = sqlite3.connect(db)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO plan_tasks ({cols}) VALUES ({marks})",
        tuple(row.values()),
    )
    conn.commit()
    conn.close()


def test_load_task_titles_reads_ids_and_titles(tmp_path):
    db = _make_db(tmp_path)
    _insert(db, plan_id="plan-x", task_id="repair-r1-06", status="completed",
            title="修复 VP-005", attempt=1, round=1, failed_vp_id="VP-005")
    _insert(db, plan_id="plan-x", task_id="1-1", status="completed",
            title="普通任务", attempt=1, round=None, failed_vp_id=None)
    _insert(db, plan_id="other", task_id="repair-r1-01", status="completed",
            title="别的计划", attempt=1, round=1, failed_vp_id="VP-001")

    titles = fh.load_task_titles(db, "plan-x")
    assert titles == {"repair-r1-06": "修复 VP-005", "1-1": "普通任务"}


def test_load_task_titles_missing_db_is_empty(tmp_path):
    assert fh.load_task_titles(tmp_path / "absent.db", "plan-x") == {}


def test_load_task_titles_and_repair_outcomes_agree(tmp_path):
    """Both read the same table through the same helper.

    ``load_repair_outcomes`` was refactored onto ``_load_plan_tasks``; a
    behaviour slip there would silently empty the "上次方案无效" prompt
    block that the repair feedback loop depends on.
    """
    db = _make_db(tmp_path)
    _insert(db, plan_id="plan-x", task_id="repair-r3-01", status="failed",
            title="老修复", test_command="pytest a",
            failure_reason="still red", attempt=2, round=3,
            failed_vp_id="VP-023")

    assert fh.load_task_titles(db, "plan-x") == {"repair-r3-01": "老修复"}
    outcomes = fh.load_repair_outcomes(db, "plan-x")
    assert outcomes["VP-023"]["repair_task_id"] == "repair-r3-01"
    assert outcomes["VP-023"]["repair_failure_reason"] == "still red"


class TestOrchestratorDiskTitles:
    """``VerificationOrchestrator._load_disk_task_titles``."""

    @staticmethod
    def _orchestrator(plan_dir: Path):
        from verification.orchestrator import VerificationOrchestrator

        orchestrator = VerificationOrchestrator.__new__(VerificationOrchestrator)
        orchestrator.plan_dir = plan_dir
        return orchestrator

    def test_reads_the_tasks_envelope(self, tmp_path):
        (tmp_path / "tasks.json").write_text(
            json.dumps({"tasks": [
                {"id": "repair-r1-06", "title": "修复 VP-005"},
                {"id": "1-1", "title": "普通任务"},
            ]}),
            encoding="utf-8",
        )
        assert self._orchestrator(tmp_path)._load_disk_task_titles() == {
            "repair-r1-06": "修复 VP-005",
            "1-1": "普通任务",
        }

    def test_reads_a_bare_list(self, tmp_path):
        (tmp_path / "tasks.json").write_text(
            json.dumps([{"id": "a", "title": "t"}]), encoding="utf-8",
        )
        assert self._orchestrator(tmp_path)._load_disk_task_titles() == {"a": "t"}

    def test_missing_file_is_empty(self, tmp_path):
        assert self._orchestrator(tmp_path)._load_disk_task_titles() == {}

    def test_corrupt_file_is_empty(self, tmp_path):
        (tmp_path / "tasks.json").write_text("{not json", encoding="utf-8")
        assert self._orchestrator(tmp_path)._load_disk_task_titles() == {}


# ---------------------------------------------------------------------------
# End to end through check_cycle_conditions
# ---------------------------------------------------------------------------

CYCLE_PLAN_ID = "test-plan"


def _write_plan_state(plan_dir: Path) -> None:
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_dir.name,
            "current_phase": "executing",
            "completed_phases": ["execution"],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {},
            "verification": {
                "status": "pending", "round": 0, "max_rounds": 3,
                "stop_reason": None,
            },
        }),
        encoding="utf-8",
    )


@pytest.fixture
def seeded_repair_rows(tmp_path, monkeypatch):
    """Hermetic state.db already holding round 1's first repair batch."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(db)
    migrate(conn)
    repo = PlanTaskRepository(conn)
    for seq, vp_id in ((1, "VP-001"), (2, "VP-002"), (3, "VP-003")):
        repo.add_task(CYCLE_PLAN_ID, {
            "id": f"repair-r1-{seq:02d}",
            "title": f"旧批次修复 {vp_id}",
            "task_group": "repair-round-1",
            "round": 1,
            "failed_vp_id": vp_id,
        })
    conn.close()
    return db


def _run_cycle(tmp_path, disk_tasks=None):
    """Drive ``check_cycle_conditions`` on a scripted one-VP failure round.

    The assembler is left REAL — it is the unit under test. Everything
    that would need a provider (the verification agent, the repair LLM)
    is stubbed.

    ``disk_tasks`` seeds ``tasks.json``; it defaults to the previous
    batch's task plus a breakdown child of it, as the executor's
    load-time reconcile would have left them.
    """
    from unittest.mock import Mock, patch

    from verification import VerificationOrchestrator

    if disk_tasks is None:
        disk_tasks = [
            {"id": "repair-r1-01", "title": "旧批次修复 VP-999"},
            {"id": "repair-r1-01-1", "title": "旧批次的拆分子任务"},
        ]

    plan_dir = tmp_path / "plans" / CYCLE_PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write_plan_state(plan_dir)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": disk_tasks}), encoding="utf-8",
    )

    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {"id": "VP-009", "status": "FAILED", "title": "新的失败点"},
        ],
        "requirement_deviations": [],
    }

    reader = Mock(return_value=[
        {"id": "VP-009", "title": "新的失败点", "priority": "high"},
    ])
    with patch(
        "verification.verification_report_reader.extract_failed_vps_with_paths",
        reader, create=True,
    ), patch(
        "verification.verification_report_reader.extract_failed_vps_from_report",
        reader, create=True,
    ), patch("verification.orchestrator.VerificationAgent"), patch(
        "verification.orchestrator.RepairTaskGenerator",
    ):
        orchestrator = VerificationOrchestrator(plan_dir, project_dir, object())
        orchestrator.verification_agent.run_full_verification.return_value = report
        orchestrator.repair_generator.generate_repair_contents.return_value = [
            {"failed_vp_id": "VP-009", "title": "修复 VP-009",
             "description": "do the thing"},
        ]
        orchestrator.start_verification_cycle(round_number=1)
        return orchestrator.check_cycle_conditions(report, round_number=1)


def test_cycle_does_not_reissue_an_id_that_is_already_taken(
    tmp_path, seeded_repair_rows, capsys,
):
    """The observed shape, through the real orchestrator.

    Three ``repair-r1-*`` rows are already in ``plan_tasks`` and
    ``repair-r1-01`` is in ``tasks.json``. Before the fix the new batch
    emitted ``repair-r1-01`` for the new failure — overwriting the row
    and re-parenting ``repair-r1-01-1`` onto an unrelated task.
    """
    result = _run_cycle(tmp_path)

    ids = [t["id"] for t in result["repair_tasks"]]
    assert ids, "the round produced no repair task at all"
    taken = {"repair-r1-01", "repair-r1-01-1",
             "repair-r1-02", "repair-r1-03"}
    assert taken.isdisjoint(ids), (
        f"the new batch reused {sorted(taken & set(ids))} — plan_tasks is "
        f"keyed on (plan_id, task_id), so those rows would be overwritten"
    )
    assert ids == ["repair-r1-04"], (
        "the batch should resume at the next free slot, not restart at 01"
    )

    out = capsys.readouterr().out
    assert "repair_task_id_store_conflict" in out or "id(s) disagree" in out, (
        "tasks.json says repair-r1-01 is '旧批次修复 VP-999' while "
        "plan_tasks says '旧批次修复 VP-001' — the cross-store check "
        "should have said so"
    )


def test_cycle_keeps_seq_one_when_nothing_is_allocated(tmp_path, monkeypatch):
    """Control: a plan's first repair batch still starts at ``01``.

    Neither store knows any repair id, so the allocation guard must be a
    no-op — otherwise every plan's first repair round silently renumbers
    and the ids stop matching what operators have in their cards.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "empty-state.db"))
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(tmp_path / "empty-state.db")
    migrate(conn)
    conn.close()

    result = _run_cycle(tmp_path, disk_tasks=[])
    assert [t["id"] for t in result["repair_tasks"]] == ["repair-r1-01"]
