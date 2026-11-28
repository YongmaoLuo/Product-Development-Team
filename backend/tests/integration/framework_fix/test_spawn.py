"""The framework-fix spawner — the envelope, the plan it creates, and the
dedup that keeps one signature to one repair plan.

What this file pins
-------------------
* ``build_envelope`` produces the arch decision point 4 envelope: the
  fingerprint, the signature, the reproduction evidence, the impact
  scope, the acceptance list and the **five** task-template steps;
* ``spawn`` creates a plan whose ``plan_kind`` is ``framework_fix``,
  whose task list is exactly those five steps, and whose envelope is on
  disk at ``<plan>/framework_fix/envelope.json``;
* ``spawn`` twice for one fingerprint returns the **same** plan id and
  leaves one plan directory behind;
* no task text carries an unfilled placeholder — every step names the
  files, the command and the assertion it is judged by.

Two boundary cases live here too, because they are the ones a spawner
gets wrong quietly:

* a fingerprint nobody is waiting on still produces a plan (the
  ``out_of_scope_change`` path — the block was decided without a
  reproduction, so there is no attach to begin with);
* a generation that fails half way leaves **no** plan directory behind.

Why these tests are *integration* tests
---------------------------------------
The spawner writes to the two places the rest of the system reads: the
plan directory and the state database.  A double for either would test
the spawner against a world nobody runs.  The database each test gets
is the shared ``state_db`` fixture — a migrated file inside that test's
own ``tmp_path``, never the operator's live ``state.db``.  The plans
root is redirected the same way, by the autouse ``isolated_plans_dir``
fixture, so a spawned plan never lands in the repository's ``plans/``.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from framework_fix.classifier import FailureHit
from framework_fix.fingerprint import compute_fingerprint
from framework_fix.registry import FrameworkFixRegistry
from framework_fix.repro import ReproAttempt, ReproductionResult
from framework_fix.spawner import (
    TASK_TEMPLATE_STEPS,
    build_envelope,
    spawn,
)


#: The words a template would leave behind if a value were missing.
#: The scan is deliberately blunt: it looks for the *shape* of an
#: unfilled slot (``TODO``, ``<...>``, ``{...}``, a bare ellipsis)
#: rather than for a vocabulary, because a template that invents its
#: own placeholder word is exactly the case this has to catch.
_PLACEHOLDER_MARKERS = (
    "TODO",
    "TBD",
    "FIXME",
    "XXX",
    "PLACEHOLDER",
    "???",
    "占位",
    "待补",
    "待定",
    "<",
    ">",
    "{",
    "}",
    "...",
)


def _hit(**overrides: Any) -> FailureHit:
    """A blocking hit, as the classifier's callers describe one."""
    hit = FailureHit(
        vp_id="VP-3",
        plan_id="plan-a",
        error_class="AssertionError",
        first_error_line="AssertionError: expected 1, got 2",
        framework_files=["backend/framework_fix/registry.py"],
        acceptance_critical=True,
        out_of_scope_change=False,
    )
    return dataclasses.replace(hit, **overrides) if overrides else hit


def _repro(**overrides: Any) -> ReproductionResult:
    """A reproduction that failed twice, the same way."""
    attempts = (
        ReproAttempt(
            run=1,
            exit_code=1,
            timed_out=False,
            first_error_line="AssertionError: expected 1, got 2",
            duration_s=1.5,
        ),
        ReproAttempt(
            run=2,
            exit_code=1,
            timed_out=False,
            first_error_line="AssertionError: expected 1, got 2",
            duration_s=1.4,
        ),
    )
    result = ReproductionResult(
        classification="framework_deterministic",
        attempts=attempts,
        clean_commit="a" * 40,
        tree_fingerprint="sha256:" + "b" * 64,
    )
    return dataclasses.replace(result, **overrides) if overrides else result


def _fingerprint() -> str:
    """The signature under repair."""
    return "sha256:" + "c" * 64


def _envelope(**overrides: Any) -> Dict[str, Any]:
    """The envelope the spawner is handed."""
    kwargs: Dict[str, Any] = {
        "fingerprint": _fingerprint(),
        "hit": _hit(),
        "repro": _repro(),
        "repo": "repo-a",
    }
    kwargs.update(overrides)
    return build_envelope(**kwargs)


def _plans_root() -> Path:
    """The plans root this test writes into."""
    from config_paths import resolve_plans_dir

    return resolve_plans_dir()


def _plan_tasks(plan_id: str) -> List[Dict[str, Any]]:
    """The task list a spawned plan carries on disk."""
    payload = json.loads(
        (_plans_root() / plan_id / "tasks.json").read_text(encoding="utf-8")
    )
    return list(payload["tasks"])


def _fingerprint_of(hit: FailureHit) -> str:
    """The fingerprint this failure's material actually produces."""
    return compute_fingerprint(
        framework_files=list(hit.framework_files),
        error_class=hit.error_class,
        first_error_line=hit.first_error_line,
    )


def _envelope_for(hit: FailureHit, repo: str = "repo-a") -> Dict[str, Any]:
    """An envelope whose fingerprint is the one its signature produces.

    The spawner compares the envelope's fingerprint with the one computed
    from the signature; an envelope that carries a made-up fingerprint would
    never reach the registry, and the registry is half of what these tests
    are about.
    """
    return build_envelope(
        fingerprint=_fingerprint_of(hit),
        hit=hit,
        repro=_repro(),
        repo=repo,
    )


def _routing_block(plan_id: str) -> Dict[str, Any]:
    """The framework-fix block the routing row carries for this plan."""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        row = conn.execute(
            "SELECT framework_fix FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"the routing row of {plan_id!r} is missing"
    return json.loads(row[0])


def _lease_rows(state_db: Path) -> List[Dict[str, Any]]:
    """Every merge-lease row, ordered by repository."""
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT repo, fix_plan_id, pr_url, acquired_at "
            "FROM framework_fix_merge_lease ORDER BY repo"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _event_types(state_db: Path) -> List[str]:
    """The audit chain's event types, oldest first."""
    conn = sqlite3.connect(str(state_db))
    try:
        rows = conn.execute(
            "SELECT event_type FROM framework_fix_event ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------
def test_envelope_shape() -> None:
    """The envelope carries the signature, the evidence and the five steps."""
    envelope = _envelope()

    assert envelope["plan_kind"] == "framework_fix"
    assert envelope["steps"] == TASK_TEMPLATE_STEPS
    assert envelope["steps"] == (
        "locate",
        "fix",
        "targeted_test",
        "gate",
        "open_pr",
    )
    assert envelope["tasks"] == 5
    assert envelope["fingerprint"] == _fingerprint()

    # The signature: what failed, where, and how.
    signature = envelope["signature"]
    assert signature["framework_files"] == ["backend/framework_fix/registry.py"]
    assert signature["error_class"] == "AssertionError"
    assert signature["first_error_line"] == "AssertionError: expected 1, got 2"

    # The reproduction evidence summary travels with the envelope: a plan
    # generated without it cannot say why it believes this is a defect.
    reproduction = envelope["reproduction"]
    assert reproduction["classification"] == "framework_deterministic"
    assert reproduction["base_commit"] == "a" * 40
    assert reproduction["tree_fingerprint"] == "sha256:" + "b" * 64
    assert len(reproduction["attempts"]) == 2
    assert reproduction["attempts"][0]["exit_code"] == 1

    # The impact scope and the acceptance list.
    assert envelope["impact_scope"]["framework_files"] == [
        "backend/framework_fix/registry.py"
    ]
    assert envelope["acceptance"], "the envelope must state what 'fixed' means"

    # One task spec per template step, in template order.
    assert [spec["title"] for spec in envelope["task_specs"]] == list(
        TASK_TEMPLATE_STEPS
    )


def test_envelope_requires_fingerprint_and_evidence() -> None:
    """An envelope without a fingerprint or without evidence is refused."""
    with pytest.raises(ValueError):
        build_envelope(
            fingerprint="",
            hit=_hit(),
            repro=_repro(),
            repo="repo-a",
        )

    with pytest.raises(ValueError):
        build_envelope(
            fingerprint=_fingerprint(),
            hit=_hit(framework_files=[]),
            repro=_repro(),
            repo="repo-a",
        )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------
def test_import_creates_plan(state_db: Path) -> None:
    """The spawned plan is a framework-fix plan with the five steps."""
    from plan_state import PlanState

    plan_id = spawn(_envelope())
    plan_dir = _plans_root() / plan_id

    assert plan_dir.is_dir()

    # The plan's kind: this is what tells the executor to start it
    # directly instead of queueing it with the ordinary batch.
    assert PlanState(plan_dir).get_plan_kind() == "framework_fix"

    # The task list is exactly the five template steps.
    tasks = _plan_tasks(plan_id)
    assert [task["title"] for task in tasks] == list(TASK_TEMPLATE_STEPS)

    # The envelope is on disk, under the plan, as the arch contract says.
    stored = json.loads(
        (plan_dir / "framework_fix" / "envelope.json").read_text(encoding="utf-8")
    )
    assert stored["fingerprint"] == _fingerprint()
    assert stored["steps"] == list(TASK_TEMPLATE_STEPS)

    # Every task works in one checkout, and that checkout is named.
    project_dirs = {task.get("project_dir") for task in tasks}
    assert len(project_dirs) == 1
    assert project_dirs.pop()


def test_no_duplicate_spawn(state_db: Path) -> None:
    """One signature produces one repair plan, however often it is asked for."""
    first = spawn(_envelope())
    second = spawn(_envelope())

    assert first == second
    assert [p.name for p in _plans_root().iterdir() if p.is_dir()] == [first]


def test_spawn_without_attached_plans(state_db: Path) -> None:
    """Nobody waiting on the defect still gets a plan (out-of-scope path)."""
    envelope = _envelope(hit=_hit(out_of_scope_change=True))
    plan_id = spawn(envelope)

    plan_dir = _plans_root() / plan_id
    assert plan_dir.is_dir()
    assert len(_plan_tasks(plan_id)) == 5

    # The attach list is empty, and the plan says so rather than
    # pretending there is somebody waiting.
    stored = json.loads(
        (plan_dir / "framework_fix" / "envelope.json").read_text(encoding="utf-8")
    )
    assert stored["attached_plans"] == []


def test_generation_failure_leaves_no_plan_directory(
    state_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spawn that fails half way leaves nothing half created."""
    import framework_fix.spawner as spawner

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the writer refused")

    monkeypatch.setattr(spawner, "_write_tasks", _boom)

    with pytest.raises(RuntimeError):
        spawn(_envelope())

    assert [p.name for p in _plans_root().iterdir() if p.is_dir()] == []


# ---------------------------------------------------------------------------
# The task text
# ---------------------------------------------------------------------------
def test_no_placeholders(state_db: Path) -> None:
    """Every step names its files, its command and its assertion."""
    plan_id = spawn(_envelope())
    tasks = _plan_tasks(plan_id)

    assert tasks, "the plan must carry tasks"

    for task in tasks:
        text = "\n".join(
            str(task.get(field, ""))
            for field in (
                "id",
                "title",
                "description",
                "test_command",
                "report_path",
            )
        )
        for marker in _PLACEHOLDER_MARKERS:
            assert marker not in text, (
                f"task {task.get('id')!r} carries the placeholder {marker!r}: "
                f"{text}"
            )

    # The marker scan alone would pass a template that says nothing at
    # all, so the concreteness is asserted too: every step names the
    # framework file it is about and the command it is judged by.
    framework_file = "backend/framework_fix/registry.py"
    for task in tasks:
        assert framework_file in task["description"], (
            f"task {task.get('id')!r} does not name the framework file it "
            "is about"
        )
        assert task["test_command"].strip(), (
            f"task {task.get('id')!r} carries no command"
        )

    # The fix step declares the framework files it may change — and
    # declares them concretely.
    fix_task = next(t for t in tasks if t["title"] == "fix")
    assert fix_task["files_to_modify"] == [framework_file]

    # The pull request step carries the fingerprint and the attach list.
    open_pr = next(t for t in tasks if t["title"] == "open_pr")
    assert _fingerprint() in open_pr["description"]


# ---------------------------------------------------------------------------
# The merge slot
# ---------------------------------------------------------------------------
# One repository has one merge slot: at most one un-merged repair PR per
# repository.  The spawner is where that rule is enforced -- a repair
# plan that opens a PR while somebody else holds the slot shifts the
# baseline under the PR that is already waiting to be merged.
def test_spawn_takes_the_repository_merge_slot(state_db: Path) -> None:
    """The repair plan holds the merge slot of the repository it repairs."""
    plan_id = spawn(_envelope())

    rows = _lease_rows(state_db)
    assert [row["repo"] for row in rows] == ["repo-a"], (
        "the repair plan must take the merge slot of its repository; got "
        f"{rows!r}"
    )
    assert [row["fix_plan_id"] for row in rows] == [plan_id], (
        "the slot belongs to the repair plan that took it; got "
        f"{[row['fix_plan_id'] for row in rows]!r}"
    )

    assert "merge_lease_acquired" in _event_types(state_db), (
        "taking the slot is a state transition and must be audited; got "
        f"{_event_types(state_db)!r}"
    )


def test_a_second_repair_in_the_same_repository_waits_for_the_slot(
    state_db: Path,
) -> None:
    """The second repair plan waits for the slot instead of taking it."""
    first = spawn(_envelope())
    second = spawn(
        _envelope_for(_hit(first_error_line="AssertionError: expected 1, got 3"))
    )

    assert first != second, (
        "a different signature is a different repair plan; got "
        f"{first!r} and {second!r}"
    )

    rows = _lease_rows(state_db)
    assert [row["fix_plan_id"] for row in rows] == [first], (
        "the slot stays with the plan that took it; got "
        f"{[row['fix_plan_id'] for row in rows]!r}"
    )

    # The waiting plan says so in its own framework-fix block: that block
    # is what the capacity ledger and the read model read.
    waiting = _routing_block(second)
    assert waiting["merge_slot"] == "waiting", (
        "the second repair plan must record that it waits for the merge "
        f"slot; got {waiting.get('merge_slot')!r}"
    )
    assert waiting["merge_slot_reason"] == "waiting_merge_slot", (
        "the reason word is the one the scheduler's closed vocabulary "
        f"carries; got {waiting.get('merge_slot_reason')!r}"
    )

    # ... and the plan that holds the slot says so too.
    assert _routing_block(first)["merge_slot"] == "held"


def test_the_registry_row_is_linked_to_the_repair_plan(
    state_db: Path,
) -> None:
    """The defect's registry row points at the repair plan -- and is audited."""
    registry = FrameworkFixRegistry(db_path=state_db)
    hit = _hit()
    fingerprint = _fingerprint_of(hit)
    registry.register_hit(
        {
            "plan_id": "plan-a",
            "vp_id": "VP-3",
            "framework_files": list(hit.framework_files),
            "error_class": hit.error_class,
            "first_error_line": hit.first_error_line,
        }
    )

    plan_id = spawn(_envelope_for(hit))

    row = registry.lookup(fingerprint)
    assert row is not None
    assert row["fix_plan_id"] == plan_id, (
        "the defect's row must point at the repair plan that fixes it; got "
        f"{row['fix_plan_id']!r}"
    )
    assert "fix_plan_linked" in _event_types(state_db), (
        "the link is a state transition and must be audited; got "
        f"{_event_types(state_db)!r}"
    )
