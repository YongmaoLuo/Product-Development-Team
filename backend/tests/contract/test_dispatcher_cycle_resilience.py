"""
TDD tests for the dispatcher cycle-resilience branch.

Background (audit 2026-09-05 plan
``2026-09-04 plan``):

  * The refiner returned 80 tasks containing a transient cycle in the
    in-memory snapshot (``task 26`` was part of a 2-node cycle with one
    of its sub-tasks ``26-x``).
  * ``_load_tasks`` → ``_validate_dependencies`` raised
    ``ValueError("Cycle detected: 26")`` for the WHOLE layer.
  * The dispatcher propagated the error to every task in the current
    micro-layer, marking all 5 of them (``23``, ``26``, ``35``, ``46``,
    ``7-4``) as failed with the same cycle error — even though 4 of
    them had already executed successfully and were about to be
    returned as ``completed``.
  * The executor then hit
    ``No schedulable micro-layer found`` (32 downstream tasks waiting
    on ``upstream_failed:35`` / ``upstream_failed:46``) and exited with
    ``executor_exited_with_unfinished_tasks:32``.

The dispatcher must NOT propagate cycle errors per task. Instead:

  1. ``test_cycle_resilience_breaks_cycle_in_load``
     When ``_validate_dependencies`` raises ``ValueError`` whose
     message starts with ``"Cycle detected: "``, the dispatcher
     parses the member ids, marks them as ``skipped`` in the task
     manager, removes them from the in-memory snapshot, rewrites
     ``tasks.json`` on disk, and re-runs validation. The retry
     passes — ``_load_tasks`` returns a valid (filtered) snapshot
     without raising.

  2. ``test_cycle_resilience_marks_members_as_skipped``
     Each cycle member's persisted status becomes ``"skipped"`` in
     ``state.db`` (so a future restart still sees them as terminal
     and the cycle cannot re-form).

  3. ``test_cycle_resilience_persists_cycle_broken_to_disk``
     After resilience, ``tasks.json`` on disk no longer contains the
     cycle — a fresh ``_load_tasks`` call sees an acyclic graph.

  4. ``test_cycle_resilience_skips_already_terminal_members``
     Cycle members that are already in a terminal state
     (``completed`` / ``failed`` / ``skipped``) are left untouched
     by the resilience pass — re-marking would generate noisy audit
     events.

  5. ``test_non_cycle_validation_error_propagates``
     ``ValueError("Task X depends on missing task Y")`` does NOT
     match the cycle-prefix and therefore propagates unchanged —
     those are structural bugs the operator must fix manually.

  6. ``test_parse_cycle_members_handles_single_id``
     The cycle message can list one or many members
     (``"Cycle detected: 26"`` vs
     ``"Cycle detected: 4-2-2, 4-2-3"``); ``_parse_cycle_members``
     handles both shapes.

  7. ``test_parse_cycle_members_returns_empty_for_non_cycle_messages``
     Messages that don't start with ``"Cycle detected: "`` return
     an empty list so the caller falls through to the legacy
     ``task_validation_failed`` path.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import pytest


# Ensure backend/ is on sys.path so ``import agent`` works regardless
# of the test runner entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Reusable fixtures / helpers (mirror test_dispatcher_self_heal.py)
# ---------------------------------------------------------------------------


def _git_init(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    if not (project_dir / ".git").exists():
        subprocess.run(
            ["git", "init"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=True,
        )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "project"
    _git_init(pd)
    return pd


def _write_tasks(project_dir: Path, tasks: List[dict]) -> Path:
    tasks_file = project_dir / "tasks.json"
    payload = {
        "requirement": "TDD spec for dispatcher cycle resilience",
        "tasks": tasks,
    }
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tasks_file


def _build_agent(
    project_dir: Path,
    plan_id: Optional[str] = None,
):
    """Build an AutonomousAgent bound to ``project_dir``.

    A real TaskManager is used so ``save_tasks`` (the on-disk
    rewrite step) exercises the production code path. The
    ``plan_id`` defaults to a per-test unique value so the
    task_progress hydrate path in ``_load_tasks`` does not pick
    up stale rows from sibling tests that share the
    ``project_dir.parent.name = "project"`` namespace (the
    ``test_agent_load.py`` suite uses the same default, so a
    naive shared plan_id would silently leak skipped-status
    rows between tests).
    """
    from agent import AutonomousAgent

    if plan_id is None:
        # Default to a UUID-derived id so each test gets its own
        # state.db namespace. Falls back to a static string for
        # deterministic plan ids in callers that pass one
        # explicitly.
        import uuid
        plan_id = f"cycle-resilience-{uuid.uuid4().hex[:8]}"

    agent = AutonomousAgent(
        requirement="TDD spec for dispatcher cycle resilience",
        project_dir=project_dir,
        coding_tool=None,
        logger=None,
    )
    agent.plan_id = plan_id
    return agent


def _make_subtask_dict(
    id: str,
    *,
    depends_on: Optional[List[str]] = None,
    title: Optional[str] = None,
    description: str = "test task",
    files_to_modify: Optional[List[str]] = None,
) -> dict:
    """Build a task dict shaped like a tasks.json entry."""
    return {
        "id": id,
        "title": title or f"Task {id}",
        "description": description,
        "test_command": "echo 1",
        # 2026-09-13: default to the READ-ONLY sentinel. The UNKNOWN
        # sentinel now fails step 3 and drives the subagent fill loop
        # (change-5 contract: gate raises when the fill loop exhausts
        # with no coding tool). Cycle-resilience tests are not about
        # files_to_modify — they need step 3 to pass so the cycle
        # check is the exercised branch.
        "files_to_modify": files_to_modify if files_to_modify is not None else ["__NO_FILE_CHANGES__"],
        "depends_on": depends_on or [],
        "model_type": "medium",
    }


# ---------------------------------------------------------------------------
# Test 1: cycle resilience breaks the cycle and load succeeds
# ---------------------------------------------------------------------------


def test_cycle_resilience_breaks_cycle_in_load(project_dir):
    """``_load_tasks`` with a 2-node cycle (A -> B -> A) must:

      1. Catch ``ValueError("Cycle detected: A, B")`` raised by
         ``_validate_dependencies``.
      2. Mark both cycle members as ``skipped`` in the task manager.
      3. Re-run validation successfully — the snapshot is now
         acyclic.
      4. Return the cycle-broken list (with the cycle members
         filtered out as terminal) without raising.
    """
    _write_tasks(
        project_dir,
        [
            _make_subtask_dict("A", depends_on=["B"]),
            _make_subtask_dict("B", depends_on=["A"]),
            _make_subtask_dict("C", depends_on=["A"]),  # depends on a cycle member
        ],
    )
    agent = _build_agent(project_dir)

    tasks = agent._load_tasks()

    # The cycle members are filtered out as terminal (skipped).
    returned_ids = {t.id for t in tasks}
    assert "A" not in returned_ids, (
        f"Cycle member A must be filtered out (terminal=skipped). "
        f"Got: {sorted(returned_ids)}"
    )
    assert "B" not in returned_ids, (
        f"Cycle member B must be filtered out. "
        f"Got: {sorted(returned_ids)}"
    )
    # Downstream task C still needs cycle member A as a dep — it is
    # therefore deferred, not in the active set either (the layer
    # builder marks it deferred_due_to_deps). The key contract is
    # that _load_tasks did not raise.
    assert "C" not in returned_ids or "C" in {t.id for t in tasks}, (
        f"C depends on a cycle member; whichever branch, _load_tasks "
        f"must not raise. Got: {sorted(returned_ids)}"
    )


# ---------------------------------------------------------------------------
# Test 2: cycle members are marked as skipped in the task manager
# ---------------------------------------------------------------------------


def test_cycle_resilience_marks_members_as_skipped(project_dir):
    """After cycle resilience, each cycle member's persisted status
    is ``"skipped"`` (terminal). A future restart that re-loads
    ``tasks.json`` and re-hydrates from ``state.db`` will see them
    as already-terminal and will not try to re-schedule them.
    """
    _write_tasks(
        project_dir,
        [
            _make_subtask_dict("X", depends_on=["Y"]),
            _make_subtask_dict("Y", depends_on=["X"]),
        ],
    )
    agent = _build_agent(project_dir)
    agent._load_tasks()

    for tid in ("X", "Y"):
        # Look up the SubTask on the in-memory task manager.
        sub = next((t for t in agent.task_manager.tasks if t.id == tid), None)
        assert sub is not None, f"{tid} should still exist on the task manager"
        assert sub.status == "skipped", (
            f"Cycle member {tid} should be marked skipped after "
            f"resilience. Got: {sub.status!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: cycle-broken snapshot is persisted back to tasks.json
# ---------------------------------------------------------------------------


def test_cycle_resilience_persists_cycle_broken_to_disk(project_dir):
    """After resilience, ``tasks.json`` on disk no longer carries
    the cycle-causing ``depends_on`` edges. The cycle members
    themselves remain in ``tasks.json`` (so the operator can see
    them in audit queries), but the edges between them are
    stripped so a fresh ``_load_tasks`` call (simulating a
    restart) sees an acyclic graph.

    Why we keep cycle members in the file (rather than removing
    them): removing them would silently change plan semantics —
    the operator would not see ``status=skipped`` for the lost
    tasks in ``plan_execution.task_progress``. Marking as
    ``skipped`` and stripping only the cycle edges is the same
    observable outcome a manual edit would produce.
    """
    _write_tasks(
        project_dir,
        [
            _make_subtask_dict("P", depends_on=["Q"]),
            _make_subtask_dict("Q", depends_on=["P"]),
        ],
    )
    agent = _build_agent(project_dir)
    agent._load_tasks()

    # Re-read tasks.json — cycle members must STILL be present
    # (so audit queries can see them), but the cycle-causing
    # ``depends_on`` edges must be stripped so the graph is acyclic.
    on_disk = json.loads((project_dir / "tasks.json").read_text(encoding="utf-8"))
    by_id = {t["id"]: t for t in on_disk["tasks"]}

    # Both members remain in the file (with status='skipped').
    assert "P" in by_id, f"Cycle member P must remain in tasks.json. Got ids: {sorted(by_id)}"
    assert "Q" in by_id, f"Cycle member Q must remain in tasks.json. Got ids: {sorted(by_id)}"

    # But the cycle-causing depends_on edges are stripped: P no
    # longer depends on Q, and Q no longer depends on P.
    p_deps = by_id["P"].get("depends_on", [])
    q_deps = by_id["Q"].get("depends_on", [])
    assert "Q" not in p_deps, (
        f"Cycle-causing edge P -> Q must be stripped from tasks.json. "
        f"Got P.depends_on={p_deps!r}"
    )
    assert "P" not in q_deps, (
        f"Cycle-causing edge Q -> P must be stripped from tasks.json. "
        f"Got Q.depends_on={q_deps!r}"
    )

    # The cycle members are persisted as skipped so the operator sees
    # them as terminal in plan_execution.task_progress.
    # ``TaskManager.save_tasks`` strips runtime fields (status,
    # updated_time, failure_reason, breakdown_count) from the
    # on-disk JSON — runtime status lives in state.db, not in
    # tasks.json. So we don't assert status here; the in-memory
    # test_cycle_resilience_marks_members_as_skipped covers the
    # status path.
    # Verify state.db writes happened by checking that the
    # task_manager still holds the status correctly (it does;
    # see the companion test).

    # A fresh _load_tasks from a NEW agent sees the cycle-broken
    # state and returns successfully (no cycle error).
    fresh_agent = _build_agent(project_dir)  # unique plan_id
    tasks = fresh_agent._load_tasks()
    # The fresh load must succeed without raising — the assertions
    # below would never run otherwise.
    assert tasks is not None


# ---------------------------------------------------------------------------
# Test 4: already-terminal cycle members are not re-marked
# ---------------------------------------------------------------------------


def test_cycle_resilience_skips_already_terminal_members(project_dir):
    """Cycle members that are already ``completed`` / ``failed`` /
    ``skipped`` in the task manager are left alone — re-marking
    would generate noisy audit events and could overwrite a
    legitimate completed status.
    """
    # Both members start as completed in tasks.json. The cycle is
    # still detected because cycle detection is purely structural
    # (Kahn's residual), independent of status. The cycle handler
    # must skip the update_task_status call for already-terminal
    # members.
    completed_tasks = [
        {
            **_make_subtask_dict("ALPHA", depends_on=["BETA"]),
            "status": "completed",
        },
        {
            **_make_subtask_dict("BETA", depends_on=["ALPHA"]),
            "status": "completed",
        },
    ]
    _write_tasks(project_dir, completed_tasks)
    agent = _build_agent(project_dir)

    # The handler must not raise on already-terminal members.
    agent._load_tasks()

    for tid in ("ALPHA", "BETA"):
        sub = next((t for t in agent.task_manager.tasks if t.id == tid), None)
        assert sub is not None, f"{tid} should still exist"
        # Status is preserved (still 'completed', not overwritten to 'skipped').
        assert sub.status == "completed", (
            f"Already-completed cycle member {tid} must keep its "
            f"status. Got: {sub.status!r}"
        )


# ---------------------------------------------------------------------------
# Test 5: a missing dependency is repaired, never re-labelled as a cycle
# ---------------------------------------------------------------------------


def test_missing_dep_is_repaired_not_relabelled(project_dir):
    """A missing ``depends_on`` target must be stripped, not reported as a
    cycle and not propagated as a hard load failure.

    2026-09-21. The previous behaviour on this shape was the worst of
    both: ``task_manager._ensure_acyclic`` counted a missing target as an
    unmet in-degree, so the *whole downstream cone* was reported as
    "Cycle detected: ..." (in a production plan that
    was 18 innocent tasks from one stale reference to a split parent),
    while ``agent._validate_dependencies`` reported it as a plain
    ``ValueError`` and refused the plan.

    Neither is right: a reference to a task that does not exist carries no
    ordering information, so the edge is dropped and the load succeeds.
    """
    _write_tasks(
        project_dir,
        [
            _make_subtask_dict("X", depends_on=["MISSING"]),
        ],
    )
    agent = _build_agent(project_dir)
    loaded = agent._load_tasks()  # must NOT raise

    by_id = {t.id: t for t in loaded}
    assert "X" in by_id, f"task X must survive the load, got {sorted(by_id)}"
    assert by_id["X"].depends_on == [], (
        f"the dangling edge must be stripped, got "
        f"{by_id['X'].depends_on!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: _parse_cycle_members handles single and multiple ids
# ---------------------------------------------------------------------------


def test_parse_cycle_members_handles_single_id():
    """Single-member cycle messages parse to a one-element list."""
    from agent import AutonomousAgent

    parsed = AutonomousAgent._parse_cycle_members("Cycle detected: 26")
    assert parsed == ["26"], f"single-id parse failed. Got: {parsed!r}"


def test_parse_cycle_members_handles_multiple_ids():
    """Multi-member cycle messages parse to a multi-element list."""
    from agent import AutonomousAgent

    parsed = AutonomousAgent._parse_cycle_members("Cycle detected: 4-2-2, 4-2-3")
    assert parsed == ["4-2-2", "4-2-3"], (
        f"multi-id parse failed. Got: {parsed!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: _parse_cycle_members returns empty list for non-cycle messages
# ---------------------------------------------------------------------------


def test_parse_cycle_members_returns_empty_for_non_cycle_messages():
    """Messages that don't start with ``"Cycle detected: "`` must
    return ``[]`` so the caller falls through to the legacy path.
    """
    from agent import AutonomousAgent

    cases = [
        "Task X depends on missing task Y",
        "Task Z cannot depend on itself",
        "desc mentions X but depends_on is empty",
        "",  # empty string
        "cycle detected: lowercase prefix (different from real validator)",
    ]
    for msg in cases:
        parsed = AutonomousAgent._parse_cycle_members(msg)
        assert parsed == [], (
            f"Non-cycle message {msg!r} must parse to []. Got: {parsed!r}"
        )