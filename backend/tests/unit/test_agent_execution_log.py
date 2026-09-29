"""
TDD tests for execution.log layer_started/layer_completed events.

Background
----------
PRD decision point 0 requires the execution log to emit layer_started
and layer_completed events so operators can audit layer transitions.
VP-014 verification requires:
  1. Diamond DAG (A→(B,C)→D) produces exactly 3 pairs of layer events
  2. Each event contains layer_index, task_ids, timestamp metadata
  3. Temporal ordering: started < completed within same layer,
     and layer_completed[N] < layer_started[N+1] (downstream gate)

All tests stub _execute_task_with_retry so no real LLM call ever happens.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _git_init(project_dir: Path) -> None:
    """Init a real git repo so GitManager can bind to it."""
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


def _write_tasks(project_dir: Path, tasks: list) -> Path:
    """Helper: write a tasks.json with the given task dicts.

    Every path declared in ``files_to_modify`` is materialised under
    ``project_dir`` first. ``TaskOutputValidator`` step 3 rejects a
    declared path that does not exist on disk, and the dispatcher's
    post-read gate turns that into a hard ``RuntimeError`` — which
    would abort ``_load_tasks`` long before any layer event is emitted.
    """
    for task in tasks:
        for raw in task.get("files_to_modify") or []:
            if not isinstance(raw, str) or raw.startswith("__"):
                continue  # sentinels are accepted verbatim by step 3
            target = project_dir / raw
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()

    tasks_file = project_dir / "tasks.json"
    payload = {
        "requirement": "TDD spec for layer log events",
        "tasks": tasks,
    }
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tasks_file


class _StubCodingTool:
    """Coding tool stub that does nothing (tests stub _execute_task_with_retry)."""

    def __init__(self):
        self.calls = []

    def query_json(self, prompt: str, system_instruction: Optional[str] = None) -> dict:
        self.calls.append({"prompt": prompt, "system_instruction": system_instruction})
        raise AssertionError(
            "query_json must NOT be called when no response is scripted "
            "(test stubs should bypass this code path)"
        )


class _RecordingLogger:
    """In-memory logger stub capturing all log events for assertions.

    Mirrors ExecutionLogger's interface: adds 'ts' field to each entry
    to match the real logger's behavior.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def _record(self, level: str, event: str, message: str, **kwargs) -> None:
        with self._lock:
            self.events.append({
                "ts": datetime.utcnow().isoformat(),
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


def _build_agent(project_dir: Path, coding_tool, logger=None):
    """Build a minimal AutonomousAgent bound to project_dir."""
    from agent import AutonomousAgent

    return AutonomousAgent(
        requirement="TDD spec for layer log events",
        project_dir=project_dir,
        coding_tool=coding_tool,
        logger=logger,
    )


def _install_event_loop_for_sync_test() -> tuple[asyncio.AbstractEventLoop, callable]:
    """Install an event loop for the main thread if one isn't already set."""
    saved_policy = asyncio.get_event_loop_policy()
    fresh_policy = asyncio.DefaultEventLoopPolicy()
    asyncio.set_event_loop_policy(fresh_policy)
    loop = fresh_policy.new_event_loop()
    fresh_policy.set_event_loop(loop)

    def _restore():
        try:
            loop.close()
        except Exception:
            pass
        try:
            asyncio.set_event_loop_policy(saved_policy)
        except Exception:
            pass

    return loop, _restore


@pytest.fixture
def project_dir(tmp_path):
    """A real project_dir with a real git repo so GitManager can bind."""
    pd = tmp_path / "project"
    _git_init(pd)
    return pd


# ---------------------------------------------------------------------------
# Test 1: Diamond DAG produces exactly 3 layer_started/layer_completed pairs
# ---------------------------------------------------------------------------


def test_layer_events_diamond_3_pairs(project_dir, monkeypatch):
    """Diamond DAG (A→(B,C)→D) must produce exactly 3 layer_started + 3 layer_completed events.

    Layer topology:
      - Layer 0: [A]
      - Layer 1: [B, C] (concurrent, both depend on A)
      - Layer 2: [D] (depends on B and C)
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "A", "title": "Task A", "description": "first",
                "test_command": "echo A", "status": "pending", "depends_on": [],
                "files_to_modify": ["a.py"],
            },
            {
                "id": "B", "title": "Task B", "description": "second-left",
                "test_command": "echo B", "status": "pending", "depends_on": ["A"],
                "files_to_modify": ["b.py"],
            },
            {
                "id": "C", "title": "Task C", "description": "second-right",
                "test_command": "echo C", "status": "pending", "depends_on": ["A"],
                "files_to_modify": ["c.py"],
            },
            {
                "id": "D", "title": "Task D", "description": "third",
                "test_command": "echo D", "status": "pending", "depends_on": ["B", "C"],
                "files_to_modify": ["d.py"],
            },
        ],
    )

    logger = _RecordingLogger()
    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool, logger=logger)
    agent._load_tasks()

    # Stub execution to immediately complete tasks
    def _stub_execute(task, max_retries=5, timeout=None):
        agent.task_manager.update_task_status(task.id, "completed")
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

    agent.run(timeout=None)

    # Filter layer events
    layer_started_events = [e for e in logger.events if e.get("event") == "layer_started"]
    layer_completed_events = [e for e in logger.events if e.get("event") == "layer_completed"]

    assert len(layer_started_events) == 3, (
        f"expected exactly 3 'layer_started' events, got {len(layer_started_events)}: "
        f"{[(e.get('data', {}).get('task_ids')) for e in layer_started_events]!r}"
    )
    assert len(layer_completed_events) == 3, (
        f"expected exactly 3 'layer_completed' events, got {len(layer_completed_events)}: "
        f"{[(e.get('data', {}).get('task_ids')) for e in layer_completed_events]!r}"
    )

    # Verify layer structure matches diamond topology
    started_ids = [e.get("data", {}).get("task_ids") for e in layer_started_events]
    completed_ids = [e.get("data", {}).get("task_ids") for e in layer_completed_events]

    # Layer 0: [A], Layer 1: [B, C], Layer 2: [D]
    assert started_ids == [["A"], ["B", "C"], ["D"]], (
        f"layer_started task_ids must be [['A'], ['B', 'C'], ['D']], got {started_ids!r}"
    )
    assert completed_ids == [["A"], ["B", "C"], ["D"]], (
        f"layer_completed task_ids must be [['A'], ['B', 'C'], ['D']], got {completed_ids!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: Layer events contain required metadata fields
# ---------------------------------------------------------------------------


def test_layer_events_contain_metadata(project_dir, monkeypatch):
    """Each layer_started/layer_completed event must contain layer_index, task_count, task_ids, and timestamp."""
    _write_tasks(
        project_dir,
        [
            {
                "id": "A", "title": "Task A", "description": "first",
                "test_command": "echo A", "status": "pending", "depends_on": [],
                "files_to_modify": ["a.py"],
            },
            {
                "id": "B", "title": "Task B", "description": "second",
                "test_command": "echo B", "status": "pending", "depends_on": ["A"],
                "files_to_modify": ["b.py"],
            },
        ],
    )

    logger = _RecordingLogger()
    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool, logger=logger)
    agent._load_tasks()

    def _stub_execute(task, max_retries=5, timeout=None):
        agent.task_manager.update_task_status(task.id, "completed")
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)
    agent.run(timeout=None)

    layer_started_events = [e for e in logger.events if e.get("event") == "layer_started"]
    layer_completed_events = [e for e in logger.events if e.get("event") == "layer_completed"]

    # Check metadata on first layer_started event
    first_started = layer_started_events[0]
    first_completed = layer_completed_events[0]

    for event, name in [(first_started, "layer_started"), (first_completed, "layer_completed")]:
        data = event.get("data", {})
        assert "layer_size" in data or "task_count" in data, (
            f"{name} event missing 'layer_size' or 'task_count' in data: {data!r}"
        )
        assert "task_ids" in data, (
            f"{name} event missing 'task_ids' in data: {data!r}"
        )
        assert "ts" in event or "timestamp" in event or "time" in data, (
            f"{name} event missing timestamp field. Event: {event!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: Temporal ordering within and across layers
# ---------------------------------------------------------------------------


def test_layer_events_temporal_order(project_dir, monkeypatch):
    """Verify: started < completed within same layer, and layer_completed[N] < layer_started[N+1].

    This is the downstream gate contract: D cannot start until B and C both complete.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "A", "title": "Task A", "description": "first",
                "test_command": "echo A", "status": "pending", "depends_on": [],
                "files_to_modify": ["a.py"],
            },
            {
                "id": "B", "title": "Task B", "description": "second-left",
                "test_command": "echo B", "status": "pending", "depends_on": ["A"],
                "files_to_modify": ["b.py"],
            },
            {
                "id": "C", "title": "Task C", "description": "second-right",
                "test_command": "echo C", "status": "pending", "depends_on": ["A"],
                "files_to_modify": ["c.py"],
            },
            {
                "id": "D", "title": "Task D", "description": "third",
                "test_command": "echo D", "status": "pending", "depends_on": ["B", "C"],
                "files_to_modify": ["d.py"],
            },
        ],
    )

    logger = _RecordingLogger()
    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool, logger=logger)
    agent._load_tasks()

    def _stub_execute(task, max_retries=5, timeout=None):
        agent.task_manager.update_task_status(task.id, "completed")
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)
    agent.run(timeout=None)

    # Pairwise ordering: layer_started[i] precedes layer_completed[i]
    all_events = logger.events
    layer_started_events = [e for e in all_events if e.get("event") == "layer_started"]
    layer_completed_events = [e for e in all_events if e.get("event") == "layer_completed"]

    # Within-layer: started[i] < completed[i]
    for i, (start_e, end_e) in enumerate(zip(layer_started_events, layer_completed_events)):
        start_ts = start_e.get("ts") or start_e.get("data", {}).get("timestamp") or start_e.get("data", {}).get("time")
        end_ts = end_e.get("ts") or end_e.get("data", {}).get("timestamp") or end_e.get("data", {}).get("time")
        if start_ts is not None and end_ts is not None:
            assert start_ts < end_ts, (
                f"layer_started[{i}] (ts={start_ts}) must precede layer_completed[{i}] (ts={end_ts})"
            )

    # Cross-layer: completed[i] < started[i+1] (downstream gate)
    for i in range(len(layer_completed_events) - 1):
        completed_e = layer_completed_events[i]
        started_e = layer_started_events[i + 1]
        completed_ts = completed_e.get("ts") or completed_e.get("data", {}).get("timestamp") or completed_e.get("data", {}).get("time")
        started_ts = started_e.get("ts") or started_e.get("data", {}).get("timestamp") or started_e.get("data", {}).get("time")
        if completed_ts is not None and started_ts is not None:
            assert completed_ts < started_ts, (
                f"layer_completed[{i}] (ts={completed_ts}) must precede layer_started[{i+1}] (ts={started_ts}) "
                f"— downstream gate was bypassed"
            )
