"""The repair task id is **allocated**, plan-globally, and never reissued.

2026-11-02 (post-mortem).  The base-drift merge task's id was
hard-coded ``repair-r<round>-01``.  ``plan_tasks`` is keyed on
``(plan_id, task_id)`` and its upsert updates the **static** columns
only — a row that already exists keeps its runtime state.  So a
hard-coded id that a previous cycle already used produced a row that
was **born finished**: the fresh task inherited the old row's
``completed`` status, the executor saw a terminal status and skipped
the task as ``superseded``, and nothing said a word about it.

The concrete accident: a plan whose round counter resets to 1 on
every cycle burned four rounds in a row, each round writing
``repair-r1-01`` / ``repair-r2-01`` / ... over rows written weeks
earlier, each round idling for seconds, until the plan went
``failed / max_rounds_reached``.

The first fix allocated the number through
:func:`repair_generator.next_repair_seq_base` — but the number was
still allocated **within a round**, and the id still carried the round
(``repair-r<round>-<seq>``).  A plan that reset its counter therefore
re-issued the very same ids one round later, and the accident happened
again (twice: on the 429 plan and on the mcode plan).

The fix that closes it: the number is allocated over the **whole plan**
and the id no longer carries the round.  ``repair-107`` — one
monotonically increasing sequence for the life of the plan.  The round
still travels with the task, in its own ``round`` field.

Old ids stay where they are.  ``repair-r2-07`` is history: it is
parsed, it is counted in the maximum, and it is never rewritten.

The properties pinned here:

* the sequence is **plan-global**: a plan that used 1…5 in round 1
  allocates ``repair-06`` in round 3 (positive control);
* the legacy shape counts too: ``repair-r2-07`` pushes the next number
  to 8 (backwards compatibility);
* the *old* rule — allocating within a round — is what this change
  removes, and it is spelled out here so the difference is decidable
  rather than implied (negative control);
* an existing ``repair-05`` in the plan pushes the new task to
  ``repair-06``;
* with no history at all the first number of the plan is used;
* ids outside the repair sequence are ignored;
* the three writers of the repair chain — the base-drift task, the
  root-cause task and the ordinary repair batch — allocate from the
  **same** counter;
* end to end, the persisted row is ``pending`` and not the old row's
  ``completed`` status.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import base_drift  # noqa: E402
import verification_agent  # noqa: E402
from base_drift import (  # noqa: E402
    BASE_DRIFT_VP_ID,
    STATUS_CONFLICTS,
    BaseDriftReport,
    build_conflict_repair_task,
)
from config_paths import resolve_state_db_path  # noqa: E402
from repair_generator import (  # noqa: E402
    RepairTaskAssembler,
    next_repair_seq_base,
    parse_repair_task_id,
)
from root_cause_task import build_root_cause_task  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)


PLAN_DIR_NAME = "plan-20261102"
PLAN_ID = PLAN_DIR_NAME
ROUND = 4
#: A legacy-shaped id: history, kept as-is, counted by the allocator.
PRIOR_TASK_ID = f"repair-r{ROUND}-01"
#: The id the plan's next merge task gets: the allocator sees sequence 1
#: already taken (by ``PRIOR_TASK_ID``) and hands out 2.
NEXT_TASK_ID = "repair-02"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _report(**over) -> BaseDriftReport:
    """A conflicting base-drift report."""
    payload = {
        "status": STATUS_CONFLICTS,
        "base_ref": "origin/main",
        "base_sha": "abc1234",
        "behind": 2,
        "ahead": 0,
        "conflict_files": [{"path": "app.py", "conflict_blocks": 1}],
        "mode": "report",
    }
    payload.update(over)
    return BaseDriftReport(**payload)


def _task(*, round_number=3, seq=None, existing_task_ids=None, report=None):
    """Build one base-drift repair task.

    ``seq`` is left out by default — that is the production path, where
    the number is allocated from ``existing_task_ids``.
    """
    kwargs = {
        "plan_id": PLAN_ID,
        "project_dir": None,
        "round_number": round_number,
    }
    if seq is not None:
        kwargs["seq"] = seq
    if existing_task_ids is not None:
        kwargs["existing_task_ids"] = existing_task_ids
    return build_conflict_repair_task(
        report if report is not None else _report(), **kwargs
    )


def _round_filtered_seq_base(existing_task_ids, round_number):
    """The pre-2026-11-02 rule, kept here so the control is decidable.

    This is the allocation rule the change removes: it looks only at the
    ids of **one** round and hands out that round's next number.  It is
    spelled out here — not imported — so the test can say "under the old
    rule the number would have been X" without depending on the
    production code still containing the old behaviour.
    """
    highest = 0
    for task_id in existing_task_ids or ():
        parsed = parse_repair_task_id(task_id)
        if parsed is None:
            continue
        task_round, seq = parsed
        if task_round != int(round_number):
            continue
        highest = max(highest, seq)
    return highest + 1


def _vp(vp_id: str):
    """A failed verification point, as the assembler wants it."""
    return {"id": vp_id, "title": f"{vp_id} title", "priority": "high"}


def _contents(vp_id: str):
    """One content row for ``vp_id``."""
    return [{
        "failed_vp_id": vp_id,
        "title": f"Fix {vp_id}",
        "description": "d",
        "files_to_modify": [f"src/{vp_id}.py"],
    }]


class _FakePersistence:
    """The persistence surface the preflight touches."""

    def __init__(self) -> None:
        self.events = []
        self.reports = []

    def write_verification_point_log(self, vp_id, event_type, data):
        self.events.append((vp_id, event_type, data))

    def update_report(self, report_data):
        self.reports.append(report_data)


def _orchestrator(tmp_path: Path):
    """An orchestrator carrying only the attributes the preflight reads."""
    orch = verification_agent.Orchestrator.__new__(
        verification_agent.Orchestrator
    )
    orch.project_dir = tmp_path
    orch.plan_dir = tmp_path / PLAN_DIR_NAME
    orch.persistence = _FakePersistence()
    orch._current_round = ROUND
    orch._last_base_drift_report = None
    return orch


def _stub_conflict(monkeypatch, report=None) -> None:
    """Make the check report the given conflict."""
    monkeypatch.setattr(
        base_drift,
        "check_and_merge_base",
        lambda *a, **k: report if report is not None else _report(),
    )


def _use_db(tmp_path: Path, monkeypatch) -> Path:
    """Point the state database at a throwaway file for this test."""
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    assert resolve_state_db_path() == db_path
    return db_path


def _seed_base_drift_row(db_path: Path, plan_id: str, task_id: str, status: str):
    """Write one base-drift merge row for ``plan_id`` in the given state.

    The row is written through the real write path — the same
    :func:`verification_agent.persist_conflict_repair_task` the preflight
    uses — so the allocator reads rows that look exactly like the ones
    it will meet in production.
    """
    task = {
        "id": task_id,
        "task_group": "repair",
        "failed_vp_id": BASE_DRIFT_VP_ID,
        "round": 4,
        "priority": "P0",
        "files_to_modify": ["app.py"],
        "title": "合并 origin/main：解决 1 个文件的冲突",
        "description": f"plan={plan_id}\n任务类型: base 合并（不是产品代码修复）",
    }
    assert verification_agent.persist_conflict_repair_task(
        plan_id, task, db_path=db_path
    )
    conn = open_db(db_path)
    try:
        PlanTaskRepository(conn).update_task(plan_id, task_id, {"status": status})
    finally:
        conn.close()


def _base_drift_rows(db_path: Path, plan_id: str):
    """``{task_id: row}`` for the base-drift rows of ``plan_id``.

    Read back through the repository, so what the test sees is what
    the executor will see.
    """
    conn = open_db(db_path)
    try:
        rows = PlanTaskRepository(conn).load_all(plan_id)
    finally:
        conn.close()
    return {
        task_id: row
        for task_id, row in rows.items()
        if row.get("failed_vp_id") == BASE_DRIFT_VP_ID
    }


# ---------------------------------------------------------------------------
# The allocation
# ---------------------------------------------------------------------------


def test_existing_id_pushes_the_new_one_up():
    """``repair-05`` exists -> the new task is ``repair-06``.

    This is the whole fix.  The old behaviour wrote the same id
    regardless of what the plan already owned, and the upsert handed
    the fresh task the old row's finished status.
    """
    task = _task(round_number=3, existing_task_ids=["repair-05"])

    assert task["id"] == "repair-06", task["id"]


def test_the_sequence_is_plan_global_not_round_scoped():
    """Round 1 used 1…5; round 3's first number is **6**.

    The id carries no round any more, so the allocation cannot either.
    A plan whose round counter resets to 1 must not start numbering
    from 1 again — that is the accident this file exists for.
    """
    existing = [f"repair-{n:02d}" for n in range(1, 6)]

    assert next_repair_seq_base(existing, 1) == 6
    assert next_repair_seq_base(existing, 3) == 6

    task = _task(round_number=3, existing_task_ids=existing)
    assert task["id"] == "repair-06", task["id"]


def test_legacy_ids_count_in_the_maximum():
    """``repair-r2-07`` is history and it still holds number 7.

    Old ids are not rewritten and not migrated — but the *number* they
    hold is still spent.  A plan written before 2026-11-02 owns
    ``repair-r2-07``; handing out number 7 again would say "the seventh
    repair of this plan" twice.
    """
    task = _task(round_number=3, existing_task_ids=["repair-r2-07"])

    assert task["id"] == "repair-08", task["id"]
    # And the legacy id still parses — it is history, not garbage.
    assert parse_repair_task_id("repair-r2-07") == (2, 7)


def test_negative_control_the_old_round_filtered_rule_reissues_a_number():
    """The rule this change removes is spelled out, and it is wrong.

    Under the old rule — allocate within the round — round 3 of a plan
    that used 1…5 in round 1 would allocate **1** again.  That number
    is already spent (``repair-r1-01``); the id would be ``repair-01``
    and the row it names would be the row round 1 already wrote.

    The control is decidable: it computes the old rule here and asserts
    that it disagrees with the production allocator — in the exact
    direction that matters (the old rule reissues a spent number, the
    new one does not).
    """
    existing = [f"repair-r1-{n:02d}" for n in range(1, 6)]

    old_rule = _round_filtered_seq_base(existing, 3)
    new_rule = next_repair_seq_base(existing, 3)

    assert old_rule == 1, "the old rule would have reissued number 1"
    assert new_rule == 6, "the new rule continues the plan's sequence"
    assert new_rule != old_rule

    # And the number the old rule would have produced is one the plan
    # already owns — in the old shape.
    assert f"repair-r1-{old_rule:02d}" in existing


def test_no_history_yields_the_first_number_of_the_plan():
    """With nothing allocated yet the plan's first number is used.

    ``repair-01`` is the shape the executor recognises and the shape
    the rest of the repair chain parses back, so the first repair task
    of a plan must keep it.
    """
    task = _task(round_number=3, existing_task_ids=[])

    assert task["id"] == "repair-01", task["id"]


def test_ids_outside_the_repair_sequence_are_ignored():
    """A task id that is not a repair id does not consume a number.

    Ordinary verification tasks share the ``plan_tasks`` table with the
    repair tasks; their ids must not shift the repair sequence.
    """
    task = _task(
        round_number=3,
        existing_task_ids=["VP-001", "some-other-task", "repair-07"],
    )

    assert task["id"] == "repair-08", task["id"]


def test_an_explicit_seq_still_wins():
    """``seq=`` keeps its old meaning: the caller names the number.

    The explicit form is what the existing callers and tests use, so it
    must not start allocating behind their back.
    """
    task = _task(round_number=3, seq=5, existing_task_ids=["repair-01"])

    assert task["id"] == "repair-05", task["id"]


# ---------------------------------------------------------------------------
# One counter for the whole repair chain
# ---------------------------------------------------------------------------


def test_the_three_writers_share_one_counter():
    """The base-drift task, the root-cause task and the repair batch
    all allocate from the same pool.

    They are three different writers in three different modules.  If
    each kept its own counter, two of them would eventually hand out the
    same number and the plan would get two tasks under one id — which is
    the accident in its purest form.

    The plan below already owns numbers 1…5 (``repair-01`` …
    ``repair-05``).  The three writers are then given that same pool and
    must take 6, 7 and 8 — in that order.
    """
    existing = [f"repair-{n:02d}" for n in range(1, 6)]

    base_drift_task = _task(round_number=3, existing_task_ids=existing)
    assert base_drift_task["id"] == "repair-06", base_drift_task["id"]

    root_cause = build_root_cause_task(
        plan_id=PLAN_ID, vp_id="VP-003", round_no=3,
        evidence={"command": "pytest tests/x.py", "error": "AssertionError"},
        existing_task_ids=existing + [base_drift_task["id"]],
    )
    assert root_cause["id"] == "repair-07", root_cause["id"]

    batch = RepairTaskAssembler(
        round_number=3,
        failed_vps=[_vp("VP-009")],
        vp_test_commands={"VP-009": "pytest -q tests/test_vp.py"},
        existing_task_ids=existing + [
            base_drift_task["id"], root_cause["id"],
        ],
    ).assemble(_contents("VP-009"))
    assert [t["id"] for t in batch] == ["repair-08"], batch


# ---------------------------------------------------------------------------
# End to end: the persisted row
# ---------------------------------------------------------------------------


def test_the_persisted_row_is_pending_not_the_old_row_status(
    monkeypatch, tmp_path,
):
    """The written row is ``pending`` — not the finished old row's status.

    This is the accident, seen from the database.  A plan already
    carries ``repair-r4-01`` from an earlier cycle, and that row is
    ``completed``.  The base-drift task the round writes must land on a
    **new** id, and its own row must start out ``pending``: a row that
    inherits ``completed`` is skipped by the executor as
    ``superseded``, and the round idles.

    The write goes through the production path — the preflight, the
    guard, the builder and the repository's upsert — with the state
    database redirected to a throwaway file.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "completed")

    orch = _orchestrator(tmp_path)
    _stub_conflict(monkeypatch)

    blocked = orch._run_base_drift_preflight()

    new_id = NEXT_TASK_ID
    # A new number, not the one the plan already owns.
    assert blocked["base_drift_repair_task_id"] == new_id, blocked
    rows = _base_drift_rows(db_path, PLAN_ID)
    assert len(rows) == 2, rows
    # The old row keeps its own finished status — it is a different task.
    assert rows[PRIOR_TASK_ID]["status"] == "completed", rows
    # The new row is the one that starts pending.
    assert rows[new_id]["status"] == "pending", rows


def test_a_three_digit_id_survives_the_task_id_allow_list(
    monkeypatch, tmp_path,
):
    """``repair-107`` must be writable.

    ``plan_tasks.task_id`` has a safe-character allow-list
    (``^[A-Za-z0-9_.:-]+$``) and a row whose id does not match it is
    **rejected**, not filtered.  Three digits is what a long-lived plan
    reaches, so the id has to survive the real write path — not just
    look right.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    task = _task(round_number=3, seq=107)

    assert task["id"] == "repair-107", task["id"]
    assert verification_agent.persist_conflict_repair_task(
        PLAN_ID, task, db_path=db_path
    ), "the row was refused"

    rows = _base_drift_rows(db_path, PLAN_ID)
    assert list(rows) == ["repair-107"], rows
    assert rows["repair-107"]["status"] == "pending", rows


def test_reusing_a_finished_id_keeps_it_finished(monkeypatch, tmp_path):
    """Why the id has to be new: the upsert keeps the old row's status.

    This pins the **mechanism** the accident rests on, so the reason for
    the allocation is written down rather than folklore:

    * writing a task id that already exists does not create a second
      row — ``plan_tasks`` is keyed on ``(plan_id, task_id)``;
    * the upsert updates the **static** columns only, so the row keeps
      the runtime state it had — here ``completed``;
    * a task that is born ``completed`` is a task the executor skips
      (it sees a terminal status and marks the task ``superseded``).

    So writing ``repair-r4-01`` again over a finished ``repair-r4-01``
    does not "retry" the merge — it silently does nothing.  The only
    way out is a **new** id, which is what the allocator hands out.
    """
    db_path = _use_db(tmp_path, monkeypatch)
    _seed_base_drift_row(db_path, PLAN_ID, PRIOR_TASK_ID, "completed")

    # The same id, written again, through the same writer.
    task = {
        "id": PRIOR_TASK_ID,
        "task_group": "repair",
        "failed_vp_id": BASE_DRIFT_VP_ID,
        "round": 4,
        "priority": "P0",
        "files_to_modify": ["app.py"],
        "title": "合并 origin/main：解决 1 个文件的冲突",
        "description": "written a second time under the very same id",
    }
    assert verification_agent.persist_conflict_repair_task(
        PLAN_ID, task, db_path=db_path
    )

    rows = _base_drift_rows(db_path, PLAN_ID)
    assert len(rows) == 1, rows
    assert rows[PRIOR_TASK_ID]["status"] == "completed", rows
