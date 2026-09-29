"""C1 (2026-09-17): every subagent attempt's conclusion is persisted.

Motivation: an attempt ran for a long time and
produced a large report; when the attempt failed, the report lived
only in the executor's memory and was lost — execution.log kept just
its character count, ``plan_artifacts`` was empty, and no transcript
existed on disk. Postmortem had nothing to read.

Contract pinned here:

* ``_persist_agent_output`` writes ``<plan>/agent_outputs/<id>_attempt<N>.md``
  with a metadata header (attempt, timestamps, parsed TEST_RESULT) and
  the raw response body.
* It never raises — a persistence failure returns ``None`` and logs
  ``task_agent_output_persist_failed``.
* The retry loop calls it for every non-None subagent response, BEFORE
  the verdict is derived (failed attempts must leave an artifact too).
* ``_persist_refiner_output`` writes the refiner's parsed structure on
  success and the error envelope on failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent import AutonomousAgent  # noqa: E402
from task_manager import TaskManager  # noqa: E402


class _StubAgent:
    """Host for the persistence helpers under test."""

    _AGENT_OUTPUTS_DIRNAME = AutonomousAgent._AGENT_OUTPUTS_DIRNAME
    _safe_output_stem = staticmethod(AutonomousAgent._safe_output_stem)
    _agent_outputs_dir = AutonomousAgent._agent_outputs_dir
    _persist_agent_output = AutonomousAgent._persist_agent_output
    _persist_refiner_output = AutonomousAgent._persist_refiner_output
    _parse_test_result = AutonomousAgent._parse_test_result

    def __init__(self, task_manager: TaskManager, logger: Optional[Any] = None):
        self.task_manager = task_manager
        self.logger = logger


class _LogRecorder:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def _record(self, level: str):
        def _fn(event: str, message: str, **kwargs: Any) -> None:
            self.events.append({
                "level": level, "event": event, "message": message,
                "task_id": kwargs.get("task_id"),
                "data": kwargs.get("data"),
            })
        return _fn

    def __getattr__(self, name: str) -> Any:
        if name in ("info", "warning", "error", "debug"):
            return self._record(name)
        raise AttributeError(name)

    def events_named(self, event: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]


PLAN_ID = "test-plan-agent-outputs"


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[tuple]:
    plan_dir = tmp_path / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    tm = TaskManager(project_dir=plan_dir, tasks_file=tasks_file)
    recorder = _LogRecorder()
    yield _StubAgent(tm, recorder), tm, plan_dir, recorder


def test_persists_response_with_metadata_header(harness) -> None:
    agent, tm, plan_dir, recorder = harness
    path = agent._persist_agent_output(
        "repair-r3-06-3", 1,
        "work done\nTEST_RESULT: FAILED\nREASON: spec contradiction",
    )
    assert path is not None
    written = Path(path)
    assert written.parent == plan_dir / "agent_outputs"
    assert written.name == "repair-r3-06-3_attempt1.md"
    text = written.read_text(encoding="utf-8")
    # Header carries the parse verdict + raw body survives verbatim.
    assert "parsed_test_result: FAILED" in text
    assert "spec contradiction" in text
    assert "task_id: repair-r3-06-3" in text
    assert recorder.events_named("task_agent_output_persisted")


def test_persists_empty_response_too(harness) -> None:
    """An empty attempt output is still an artifact (proves the attempt ran)."""
    agent, tm, plan_dir, recorder = harness
    path = agent._persist_agent_output("11-5-1", 2, "")
    assert path is not None
    text = Path(path).read_text(encoding="utf-8")
    assert "response_chars: 0" in text
    assert "parsed_test_result: FAILED" in text  # no TEST_RESULT marker


def test_none_response_is_guarded(harness) -> None:
    agent, tm, plan_dir, recorder = harness
    path = agent._persist_agent_output("t-1", 1, None)  # type: ignore[arg-type]
    assert path is not None
    assert "response_chars: 0" in Path(path).read_text(encoding="utf-8")


def test_persistence_failure_returns_none_and_logs(harness, monkeypatch) -> None:
    agent, tm, plan_dir, recorder = harness

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _boom)
    result = agent._persist_agent_output("t-1", 1, "hello")
    assert result is None
    assert recorder.events_named("task_agent_output_persist_failed")


def test_task_id_is_sanitized(harness) -> None:
    agent, tm, plan_dir, recorder = harness
    path = agent._persist_agent_output("task/with:bad*chars?", 1, "x")
    assert path is not None
    name = Path(path).name
    assert "/" not in name
    assert ".." not in name


def test_refiner_result_persisted_as_json(harness) -> None:
    agent, tm, plan_dir, recorder = harness
    path = agent._persist_refiner_output(
        "repair-r3-06-3",
        {"kind": "refiner_result", "updated_tasks": [{"id": "a"}]},
    )
    assert path is not None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["kind"] == "refiner_result"
    assert data["task_id"] == "repair-r3-06-3"
    assert data["updated_tasks"] == [{"id": "a"}]
    assert "persisted_at" in data
    assert recorder.events_named("refiner_output_persisted")


def test_refiner_error_envelope_uses_suffix(harness) -> None:
    agent, tm, plan_dir, recorder = harness
    path = agent._persist_refiner_output(
        "repair-r3-06-3", {"kind": "refiner_error", "error": "boom"}, suffix="_error",
    )
    assert path is not None
    assert Path(path).name == "refiner_repair-r3-06-3_error.json"
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["kind"] == "refiner_error"


def test_no_tasks_file_means_no_crash(harness) -> None:
    agent, tm, plan_dir, recorder = harness
    agent.task_manager.tasks_file = None  # type: ignore[assignment]
    assert agent._persist_agent_output("t-1", 1, "x") is None
    assert agent._persist_refiner_output("t-1", {"kind": "k"}) is None


def test_mock_tasks_file_does_not_write_into_the_repo(harness) -> None:
    """A MagicMock tasks_file must not materialise ``MagicMock/...`` on disk.

    ``Path(MagicMock())`` stringifies to ``MagicMock/mock.tasks_file`` and
    ``mkdir(parents=True)`` then creates that tree relative to the cwd —
    which, under pytest, is the repository. Several existing harnesses
    build a ``TaskManager`` with a mocked ``tasks_file``; without the
    isinstance guard they litter the repo on every run.
    """
    from unittest.mock import MagicMock

    agent, tm, plan_dir, recorder = harness
    agent.task_manager.tasks_file = MagicMock()  # type: ignore[assignment]
    assert agent._agent_outputs_dir() is None
    assert agent._persist_agent_output("t-1", 1, "x") is None
    assert agent._persist_refiner_output("t-1", {"kind": "k"}) is None


# ---------------------------------------------------------------------------
# Call sites must stay wired (a helper nothing calls is not observability)
# ---------------------------------------------------------------------------


def test_executor_retry_loop_invokes_the_persist_helper() -> None:
    source = (BACKEND_DIR / "agent.py").read_text(encoding="utf-8")
    assert "self._persist_agent_output(task.id, attempt + 1, coder_response)" in source, (
        "the retry loop no longer persists subagent output — failed "
        "attempts would discard their conclusion again"
    )


def test_refine_after_failure_persists_both_outcomes() -> None:
    source = (BACKEND_DIR / "agent.py").read_text(encoding="utf-8")
    assert 'suffix="_error"' in source, "refiner error envelope is no longer written"
    assert '"kind": "refiner_result"' in source, "refiner result is no longer written"

