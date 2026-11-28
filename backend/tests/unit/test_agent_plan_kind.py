"""The executor's one framework-fix seam — ``plan_kind`` and the direct start.

What this file pins
-------------------
PRD decision point 8 and architecture decision point 1: the executor is
allowed exactly **one** judgment about the framework-fix lane — whether
the plan it is running is a framework-fix plan.  That judgment exists so
a framework-fix plan can start its current task on its own instead of
waiting for the ordinary batch dispatch.  Everything else about how a
plan runs stays exactly as it was: no second execution path, no new
abstraction layer, no new scheduling mechanism.

The kind is read from the plan's routing row — the same column every
other reader of a plan's kind reads (``PlanState.get_plan_kind``, task
17).  A plan that says nothing about its kind is an ordinary plan: the
framework-fix lane did not exist when it was written, so reading it as
anything else would invent a distinction the plan never made.

TDD contract — five cases
-------------------------
1. ``test_framework_plan_starts_directly`` — a plan whose routing row
   says ``framework_fix`` starts its current task on its own: the
   ordinary batch is never assembled, so two independent tasks do **not**
   overlap.
2. ``test_normal_plan_unchanged`` — the same plan with the ordinary kind
   still runs as one concurrent batch.  This is the regression guard:
   the seam must not touch the ordinary path.
3. ``test_missing_kind_defaults`` — a plan that says nothing about its
   kind is an ordinary plan.
4. ``test_read_failure_raises`` — a kind that cannot be read raises; it
   is never silently degraded into ``normal`` (the ordinary path is the
   one that waits, so guessing would quietly change what the plan does).
5. ``test_only_one_plan_kind_judgment`` — the executor holds exactly one
   comparison between a plan's kind and the framework-fix kind.

The executor is driven for real: a real ``tasks.json``, a real routing
row in a real (temporary) SQLite database, and the real dispatch loop.
Only the LLM call is stubbed — ``_execute_task_with_retry`` is replaced
by a recorder, exactly as the other dispatch tests do it.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


# Ensure backend/ is on sys.path so `import agent` works regardless of
# which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: The kind a framework-fix plan carries.
_FRAMEWORK_FIX_KIND = "framework_fix"

#: The kind every other plan carries.
_ORDINARY_KIND = "normal"


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _git_init(project_dir: Path) -> None:
    """Init a real git repo so ``GitManager`` can bind to it."""
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
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


def _write_tasks(
    project_dir: Path,
    plan_dir: Path,
    tasks: List[Dict[str, Any]],
) -> Path:
    """Write the plan's ``tasks.json`` and materialise its declared files.

    ``AutonomousAgent._load_tasks`` runs the dispatcher post-read gate
    (``TaskOutputValidator``): step 3 of that gate requires every
    declared ``files_to_modify`` path to exist on disk, and rejects an
    empty list outright.  These fixtures are about the dispatch shape,
    not about file attribution, so every declared path is materialised
    under ``project_dir``.
    """
    normalised = []
    materialise: List[str] = []
    for task in tasks:
        task = dict(task)
        if not task.get("files_to_modify"):
            task["files_to_modify"] = [f"src/{task.get('id', 'task')}.py"]
        materialise.extend(task["files_to_modify"])
        normalised.append(task)

    for raw in materialise:
        if not isinstance(raw, str) or raw.startswith("__"):
            continue  # sentinels are accepted verbatim by step 3
        path = Path(raw)
        if not path.is_absolute():
            path = project_dir / raw
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks_file = plan_dir / "tasks.json"
    payload = {
        "requirement": "TDD spec for the framework-fix executor seam",
        "tasks": normalised,
    }
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tasks_file


def _seed_plan_kind(plan_id: str, kind: Optional[str]) -> None:
    """Write the plan's routing row, with (or without) a ``plan_kind``.

    The row is written through the production write path (the schema
    migration plus plain SQL for the column itself), because the column
    the executor reads is exactly the one that lives there.
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, version, updated_at) "
            "VALUES (?, ?, 0, ?)",
            (plan_id, "executing", "2026-11-23T00:00:00Z"),
        )
        if kind is not None:
            conn.execute(
                "UPDATE plan_routing SET plan_kind = ? WHERE plan_id = ?",
                (kind, plan_id),
            )
        conn.commit()
    finally:
        conn.close()


class _StubCodingTool:
    """Coding tool stub — the dispatch tests never call it."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def query_json(
        self, prompt: str, system_instruction: Optional[str] = None
    ) -> dict:
        self.calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
        })
        raise AssertionError(
            "query_json must NOT be called — the dispatch tests stub "
            "_execute_task_with_retry instead"
        )


class _RecordingLogger:
    """In-memory logger stub capturing every log event."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def _record(self, level: str, event: str, message: str, **kwargs) -> None:
        with self._lock:
            self.events.append({
                "level": level,
                "event": event,
                "message": message,
                **kwargs,
            })

    def debug(self, event: str, message: str, **kwargs) -> None:
        self._record("DEBUG", event, message, **kwargs)

    def info(self, event: str, message: str, **kwargs) -> None:
        self._record("INFO", event, message, **kwargs)

    def warning(self, event: str, message: str, **kwargs) -> None:
        self._record("WARNING", event, message, **kwargs)

    def error(self, event: str, message: str, **kwargs) -> None:
        self._record("ERROR", event, message, **kwargs)

    def critical(self, event: str, message: str, **kwargs) -> None:
        self._record("CRITICAL", event, message, **kwargs)

    def events_named(self, name: str) -> List[Dict[str, Any]]:
        """Every recorded event with this name."""
        with self._lock:
            return [e for e in self.events if e.get("event") == name]


def _build_agent(project_dir: Path, tasks_file: Path, logger=None):
    """Build a minimal ``AutonomousAgent`` bound to this plan."""
    from agent import AutonomousAgent

    return AutonomousAgent(
        requirement="TDD spec for the framework-fix executor seam",
        project_dir=project_dir,
        coding_tool=_StubCodingTool(),
        logger=logger,
        tasks_file=tasks_file,
    )


def _two_independent_tasks() -> List[Dict[str, Any]]:
    """Two tasks with no dependency between them.

    Two tasks with no dependency are the smallest shape that tells the
    two dispatch shapes apart: an ordinary plan puts them in one batch
    and runs them concurrently, while a plan that starts its current
    task on its own runs them one after the other.
    """
    return [
        {
            "id": "A",
            "title": "First slice",
            "description": "the first independent slice",
            "test_command": "echo A",
            "status": "pending",
            "depends_on": [],
            "files_to_modify": ["a.py"],
        },
        {
            "id": "B",
            "title": "Second slice",
            "description": "the second independent slice",
            "test_command": "echo B",
            "status": "pending",
            "depends_on": [],
            "files_to_modify": ["b.py"],
        },
    ]


def _record_dispatch(agent, timings: Dict[str, Dict[str, float]], lock) -> None:
    """Install a recorder in place of the real task execution."""

    def _stub_execute(task, max_retries=5, timeout=None):
        with lock:
            timings[task.id] = {"start": time.monotonic()}
        time.sleep(0.05)
        with lock:
            timings[task.id]["end"] = time.monotonic()
        agent.task_manager.update_task_status(task.id, "completed")
        return True

    agent._execute_task_with_retry = _stub_execute


def _overlaps(a: Dict[str, float], b: Dict[str, float]) -> bool:
    """Do two tasks' execution windows overlap in time?"""
    return a["start"] < b["end"] and b["start"] < a["end"]


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A real project_dir with a real git repo so GitManager can bind."""
    pd = tmp_path / "project"
    _git_init(pd)
    return pd


# ---------------------------------------------------------------------------
# 1. A framework-fix plan starts its current task directly
# ---------------------------------------------------------------------------
def test_framework_plan_starts_directly(
    state_db: Path, project_dir: Path, tmp_path: Path
) -> None:
    """A framework-fix plan does not wait for the ordinary batch.

    The plan carries two independent tasks.  An ordinary plan puts them
    in one batch and runs them concurrently; a framework-fix plan starts
    its current task on its own, so the two never overlap — and the
    dispatch log shows two single-task layers instead of one two-task
    layer.
    """
    plan_id = "fixplan-001"
    plan_dir = tmp_path / plan_id
    tasks_file = _write_tasks(project_dir, plan_dir, _two_independent_tasks())
    _seed_plan_kind(plan_id, _FRAMEWORK_FIX_KIND)

    logger = _RecordingLogger()
    agent = _build_agent(project_dir, tasks_file, logger=logger)
    agent._load_tasks()

    # The seam's own answer, read the way the executor reads it.
    decision = agent._plan_start_decision()
    assert decision == {
        "plan_kind": _FRAMEWORK_FIX_KIND,
        "direct_start": True,
    }, f"a framework-fix plan must start directly, got {decision!r}"

    timings: Dict[str, Dict[str, float]] = {}
    lock = threading.Lock()
    _record_dispatch(agent, timings, lock)

    agent.run(timeout=None)

    assert set(timings) == {"A", "B"}, (
        f"both tasks must run, got {sorted(timings)!r}"
    )
    assert not _overlaps(timings["A"], timings["B"]), (
        "the framework-fix plan waited for the batch: A and B overlapped "
        f"(A={timings['A']!r}, B={timings['B']!r})"
    )

    layer_sizes = [
        e.get("data", {}).get("layer_size")
        for e in logger.events_named("layer_started")
    ]
    assert layer_sizes == [1, 1], (
        "the framework-fix plan must dispatch one task at a time, got "
        f"layer sizes {layer_sizes!r}"
    )


# ---------------------------------------------------------------------------
# 2. An ordinary plan is untouched
# ---------------------------------------------------------------------------
def test_normal_plan_unchanged(
    state_db: Path, project_dir: Path, tmp_path: Path
) -> None:
    """An ordinary plan still runs its whole micro-layer as one batch."""
    plan_id = "normalplan-001"
    plan_dir = tmp_path / plan_id
    tasks_file = _write_tasks(project_dir, plan_dir, _two_independent_tasks())
    _seed_plan_kind(plan_id, _ORDINARY_KIND)

    logger = _RecordingLogger()
    agent = _build_agent(project_dir, tasks_file, logger=logger)
    agent._load_tasks()

    decision = agent._plan_start_decision()
    assert decision == {
        "plan_kind": _ORDINARY_KIND,
        "direct_start": False,
    }, f"an ordinary plan must not start directly, got {decision!r}"

    timings: Dict[str, Dict[str, float]] = {}
    lock = threading.Lock()
    _record_dispatch(agent, timings, lock)

    agent.run(timeout=None)

    assert set(timings) == {"A", "B"}, (
        f"both tasks must run, got {sorted(timings)!r}"
    )
    assert _overlaps(timings["A"], timings["B"]), (
        "the ordinary plan lost its concurrent batch: A and B did not "
        f"overlap (A={timings['A']!r}, B={timings['B']!r})"
    )

    layer_sizes = [
        e.get("data", {}).get("layer_size")
        for e in logger.events_named("layer_started")
    ]
    assert layer_sizes == [2], (
        "the ordinary plan must still dispatch its micro-layer as one "
        f"batch, got layer sizes {layer_sizes!r}"
    )


# ---------------------------------------------------------------------------
# 3. A plan that says nothing about its kind is an ordinary plan
# ---------------------------------------------------------------------------
def test_missing_kind_defaults(
    state_db: Path, project_dir: Path, tmp_path: Path
) -> None:
    """No ``plan_kind`` at all → ``normal``, and the ordinary path.

    A plan written before the framework-fix lane existed carries no
    kind.  Reading it as anything else would invent a distinction the
    plan never made — and the ordinary path is the one that waits.
    """
    plan_id = "legacyplan-001"
    plan_dir = tmp_path / plan_id
    tasks_file = _write_tasks(project_dir, plan_dir, _two_independent_tasks())
    # A routing row WITHOUT the kind column set — the shape every plan
    # that predates the lane has.
    _seed_plan_kind(plan_id, None)

    logger = _RecordingLogger()
    agent = _build_agent(project_dir, tasks_file, logger=logger)
    agent._load_tasks()

    decision = agent._plan_start_decision()
    assert decision == {
        "plan_kind": _ORDINARY_KIND,
        "direct_start": False,
    }, f"a plan without a kind is an ordinary plan, got {decision!r}"

    timings: Dict[str, Dict[str, float]] = {}
    lock = threading.Lock()
    _record_dispatch(agent, timings, lock)

    agent.run(timeout=None)

    assert _overlaps(timings["A"], timings["B"]), (
        "a plan without a kind must keep the ordinary concurrent batch: "
        f"A and B did not overlap (A={timings['A']!r}, B={timings['B']!r})"
    )


# ---------------------------------------------------------------------------
# 4. A kind that cannot be read raises
# ---------------------------------------------------------------------------
def test_read_failure_raises(
    state_db: Path, project_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    """A failed read is an error, not a silent ``normal``.

    The ordinary path is the one that waits for the batch.  Degrading a
    failed read into ``normal`` would quietly change what the plan does
    — and the plan would never learn that its kind was never read.
    """
    import agent as agent_module

    plan_id = "brokenread-001"
    plan_dir = tmp_path / plan_id
    tasks_file = _write_tasks(project_dir, plan_dir, _two_independent_tasks())
    _seed_plan_kind(plan_id, _FRAMEWORK_FIX_KIND)

    agent = _build_agent(project_dir, tasks_file)
    agent._load_tasks()

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("the state database cannot be read")

    monkeypatch.setattr(agent_module, "PlanState", _Boom)

    with pytest.raises(RuntimeError):
        agent._plan_start_decision()


# ---------------------------------------------------------------------------
# 5. The executor holds exactly one plan-kind judgment
# ---------------------------------------------------------------------------
def test_only_one_plan_kind_judgment() -> None:
    """``agent.py`` compares a plan's kind with the framework-fix kind once.

    The executor's framework-fix awareness is ONE judgment (PRD decision
    point 8).  A second comparison would be a second execution path
    wearing a different hat, and the whole point of the lane is that the
    framework-fix plan reuses the ordinary executor.
    """
    agent_path = _BACKEND_DIR / "agent.py"
    tree = ast.parse(agent_path.read_text(encoding="utf-8"))

    matches: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, ast.Eq) for op in node.ops):
            continue
        operands = [node.left] + list(node.comparators)
        kinds = [
            operand.value
            for operand in operands
            if isinstance(operand, ast.Constant)
            and operand.value == _FRAMEWORK_FIX_KIND
        ]
        if not kinds:
            continue
        names = [
            operand.id
            for operand in operands
            if isinstance(operand, ast.Name)
            and "plan_kind" in operand.id
        ]
        if names:
            matches.append(f"{agent_path.name}:{node.lineno}")

    assert len(matches) == 1, (
        "the executor must hold exactly one plan-kind judgment, found "
        f"{len(matches)}: {matches!r}"
    )
