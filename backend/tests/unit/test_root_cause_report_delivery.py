"""A root-cause task owes a REPORT, and the executor has to SEE that.

2026-11-03. The root-cause chain (VP-017, round 3) dispatched a
diagnosis task whose entire deliverable is one markdown file at
``plans/<plan>/reports/root-cause-<vp>.md``. Three layers of the
framework disagreed about that fact, and the disagreement was silent:

1. :func:`root_cause_task.build_root_cause_task` wrote ``deliverable``
   as the FIVE-ITEM LIST of what the report must contain. The task
   model's ``deliverable`` is ``Literal["code", "report"]`` — so the
   row could not be parsed as a task at all (pydantic refuses a list),
   and every consumer that saw the row treat it as "not ``report``"
   concluded "a code task".
2. The persistence allow-list
   (``state_machine.repositories.plan_task_repository.ALLOWED_STATIC_TASK_FIELDS``)
   carries neither ``deliverable`` nor ``report_path``: the
   ``plan_tasks`` table has no column for them. The diagnosis task
   therefore reaches the executor as a plain code task — with the
   report's address written in its description and nothing else.
3. The executor's own loader (``AutonomousAgent._load_tasks``) reads
   the row and, with nothing to hydrate from, produces a task whose
   deliverable is the ``"code"`` default.

The result: the report was written, and the executor asked the wrong
question about it ("did the repo change?"), answered "no", and retried
the task with ``empty_diff_no_changes`` — forever, because a diagnosis
task declares no ``files_to_modify`` and its deliverable is by
definition not a diff.

What the framework already had, unused: the whole report-task
machinery (``is_report_task`` / ``resolve_report_path`` /
``read_report_deliverable`` / the write guard's ``PDT_REPORT_PATH``
carve-out) has been in place since 2026-10-19 — and a root-cause row
never reached it.

What is pinned here:

  * the BUILDER declares a report deliverable (and keeps the five
    required sections, which are the report's CONTENT, not its type);
  * the executor's LOADER recognises a persisted root-cause row and
    hydrates the report contract back onto it, pointing at the plan's
    own ``reports/`` directory — which is where the chain reads the
    report from (``verification.orchestrator._root_cause_report_file``).

The negative control matters as much as the positive case: the
IMPLEMENTATION task's description names the SAME report file ("按
<report> 的根因结论修复 ..."), so a loader that keyed on the path
alone would turn the code task that fixes the bug into a task that
owes a report. A diagnosis task is recognised by the pair "the
description names a root-cause report" AND "the row declares no files
to modify" — the second half is the documented diagnosis contract
(:mod:`root_cause_task`: a root-cause task has no ``files_to_modify``
because its output is a judgement, not a change).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from root_cause_task import (  # noqa: E402
    ROOT_CAUSE_DELIVERABLE,
    build_root_cause_task,
    root_cause_report_absolute_path,
)
from task import SubTask  # noqa: E402

PLAN_ID = "20261102-nightly"
VP_ID = "VP-017"
REPORT_NAME = f"root-cause-{VP_ID}.md"
REPORT_REL = f"plans/{PLAN_ID}/reports/{REPORT_NAME}"


# ---------------------------------------------------------------------------
# 1 — the builder
# ---------------------------------------------------------------------------


def test_a_root_cause_task_declares_a_report_deliverable():
    """``deliverable`` is the task's TYPE; the five requirements are its CONTENT.

    Both used to be written to the same key, so the task model refused
    the row: ``deliverable`` is ``Literal["code", "report"]`` and a
    five-item list is neither. The report-task contract already existed
    (``deliverable="report"`` + ``report_path``); the builder simply
    never used it.
    """
    row = build_root_cause_task(PLAN_ID, VP_ID, 3, task_id="repair-04")

    assert row["deliverable"] == "report"
    report_path = row["report_path"]
    # 2026-11-03 — the report's address is ABSOLUTE, anchored on the
    # PLAN's own directory.  A relative address would be resolved by
    # the executor against its own working directory and point at a
    # different file than the one the orchestrator reads back.
    assert os.path.isabs(report_path)
    # Its SHAPE is still the documented one — ``plans/<plan>/reports/
    # <report>`` — just rooted somewhere concrete (no machine-specific
    # prefix is written down here).
    assert Path(report_path).parts[-4:] == (
        "plans", PLAN_ID, "reports", REPORT_NAME,
    )
    # One anchor, one source of truth: the very helper the builder
    # called to compute it.
    assert report_path == root_cause_report_absolute_path(PLAN_ID, VP_ID)
    # The five required sections survive — they are what the report has
    # to contain, which is a different question from what the task IS.
    assert row["report_deliverables"] == list(ROOT_CAUSE_DELIVERABLE)


def test_a_root_cause_row_is_parsable_as_a_task():
    """The row has to survive ``SubTask(**row)``.

    It did not: ``deliverable`` was the five-item list, so pydantic
    raised. The framework could not hold its own root-cause task.
    """
    row = build_root_cause_task(PLAN_ID, VP_ID, 3, task_id="repair-04")

    task = SubTask(**row)

    assert task.deliverable == "report"
    # Same contract as the row above: absolute, anchored on the plan's
    # own directory, and derived from the one helper.
    assert os.path.isabs(task.report_path)
    assert Path(task.report_path).parts[-4:] == (
        "plans", PLAN_ID, "reports", REPORT_NAME,
    )
    assert task.report_path == root_cause_report_absolute_path(PLAN_ID, VP_ID)


# ---------------------------------------------------------------------------
# 2 — the executor's loader
# ---------------------------------------------------------------------------


def _persisted_root_cause_row() -> dict:
    """The row as the orchestrator's persistence leaves it on disk.

    ``ALLOWED_STATIC_TASK_FIELDS`` carries neither ``deliverable`` nor
    ``report_path`` (the ``plan_tasks`` table has no column for them),
    so the executor sees a row whose text names the report and whose
    fields say nothing about a deliverable.
    """
    row = build_root_cause_task(PLAN_ID, VP_ID, 3, task_id="repair-04")
    persisted = {
        key: row[key]
        for key in (
            "id", "title", "description", "test_command", "model_type",
            "task_group", "priority", "status",
        )
        if key in row
    }
    persisted["project_dir"] = None
    return persisted


def _load_agent_for(tmp_path, monkeypatch, rows):
    """Build an agent over a real plan ``tasks.json`` and load its tasks.

    Drives the production entry: the agent reads the plan through the
    same ``TaskRepository`` / ``_TASK_FIELDS`` path the executor runs
    on. ``PDT_STATE_DB_PATH`` is repointed so Phase 2's orphan
    reconcile cannot merge rows from the canonical repo-root
    ``state.db`` into the assertion.
    """
    from agent import AutonomousAgent

    class _DummyCodingTool:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setenv(
        "PDT_STATE_DB_PATH", str(tmp_path / "test_state.db")
    )

    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    # GitManager binds with search_parent_directories=True; without a
    # repo here it would walk up to this checkout.
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )

    plan_dir = tmp_path / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text(
        json.dumps({"requirement": "root cause delivery", "tasks": rows}),
        encoding="utf-8",
    )

    agent = AutonomousAgent(
        requirement="root cause delivery",
        project_dir=project_dir,
        coding_tool=_DummyCodingTool(),
        logger=None,
        tasks_file=tasks_file,
    )
    return agent, agent._load_tasks(), plan_dir


def test_the_executor_loader_hydrates_a_root_cause_row_into_a_report_task(
    tmp_path, monkeypatch
):
    """A persisted diagnosis row must load as the report task it is.

    Asserted through ``is_report_task`` — the predicate the executor's
    dispatch actually branches on — rather than through the attribute
    alone, because the attribute can be present while a consumer reads
    it through a different path.
    """
    from agent import is_report_task

    _, loaded, plan_dir = _load_agent_for(
        tmp_path, monkeypatch, [_persisted_root_cause_row()],
    )

    assert len(loaded) == 1
    task = loaded[0]
    assert is_report_task(task) is True
    # The address is the PLAN's own reports directory — the same place
    # the chain reads the report from
    # (``verification.orchestrator._root_cause_report_file``), not the
    # project repo.
    assert task.report_path == str(plan_dir / "reports" / REPORT_NAME)


def test_the_loader_leaves_the_implementation_task_a_code_task(
    tmp_path, monkeypatch
):
    """The negative control: the SAME report path, in a different task.

    ``build_implementation_task`` writes "按 <report> 的根因结论修复
    <vp>。" — it names the root-cause report because that report is
    its input. A loader that keyed on the path alone would declare the
    code task that fixes the bug to be a task that owes a report, and
    the fix would never be made.
    """
    from agent import is_report_task

    row = {
        "id": "repair-05",
        "title": "实现 VP-017 的根因修复方案",
        "description": (
            f"按 {REPORT_REL} 的根因结论修复 {VP_ID}。"
            f"改动范围取自 {REPORT_REL}。"
        ),
        "test_command": "",
        "model_type": "medium",
        "project_dir": None,
        "task_group": "repair-round-3",
        "files_to_modify": ["backend/tests/unit/test_example.py"],
    }

    _, loaded, _ = _load_agent_for(tmp_path, monkeypatch, [row])

    assert len(loaded) == 1
    task = loaded[0]
    assert is_report_task(task) is False
    assert task.deliverable == "code"
    assert task.report_path is None
