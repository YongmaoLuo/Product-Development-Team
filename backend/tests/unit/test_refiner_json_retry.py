"""C3 (2026-09-17): an unparseable refiner reply gets re-asked.

Motivation: on an earlier plan the refiner answered a large prompt with
prose, ``query_json`` raised ``JSONDecodeError: no JSON object / array
boundaries found``, and ``Refiner.refine`` had no retry on that path —
the exception fell into the generic handler, the task list came back
unchanged, and the caller logged ``refine_no_change`` as though the
refiner had decided to split nothing. The dispatcher then found no
schedulable task and stopped the run while four tasks were still pending.

Contract pinned here:

* a parse failure re-asks up to ``MAX_REFINER_JSON_RETRIES`` times;
* the re-ask appends an explicit JSON-only instruction;
* recovery is logged as ``refiner_json_retry`` + the round proceeds;
* when every attempt fails the error still propagates to the caller
  (behaviour preserved) and is logged as ``refine_exception`` rather
  than silently masquerading as a no-op.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from refiner import MAX_REFINER_JSON_RETRIES, TaskRefiner  # noqa: E402


class _LogRecorder:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def _record(self, level: str):
        def _fn(event: str, message: str, **kwargs: Any) -> None:
            self.events.append({
                "level": level, "event": event, "message": message,
                "task_id": kwargs.get("task_id"), "data": kwargs.get("data"),
            })
        return _fn

    def __getattr__(self, name: str) -> Any:
        if name in ("info", "warning", "error", "debug"):
            return self._record(name)
        raise AttributeError(name)

    def events_named(self, event: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]


class _StubCodingTool:
    """Scripted ``query_json``: raises for Exception entries, else returns."""

    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.prompts: List[str] = []

    def query_json(self, prompt, system_instruction=None, scene=None, **kwargs):
        self.prompts.append(prompt)
        item = self.responses.pop(0) if self.responses else {"tasks": []}
        if isinstance(item, Exception):
            raise item
        return item


def _decode_error() -> json.JSONDecodeError:
    return json.JSONDecodeError("no JSON object / array boundaries found", "", 0)


def _refiner(responses: List[Any], logger: Optional[Any] = None) -> TaskRefiner:
    r = TaskRefiner.__new__(TaskRefiner)  # bypass AgentConfig wiring
    r.coding_tool = _StubCodingTool(responses)
    r.logger = logger
    r.config = type("C", (), {
        "refiner_system_prompt": "sys",
        "domain_knowledge": "",
    })()
    r.project_dir = None  # no validator → returns the candidate directly
    return r


TASK = {"id": "t1", "title": "t", "description": "d", "status": "pending"}


def test_unparseable_reply_is_reasked_and_recovers() -> None:
    logger = _LogRecorder()
    r = _refiner([_decode_error(), {"tasks": [TASK]}], logger)

    out = r.refine(requirement="req", tasks=[TASK], last_coder_response="c",
                   last_result="res", exit_code=1, last_task_id="t1")

    assert [t["id"] for t in out] == ["t1"]
    assert len(r.coding_tool.prompts) == 2
    assert "JSON only" in r.coding_tool.prompts[1] or "SINGLE JSON object" in r.coding_tool.prompts[1]
    retries = logger.events_named("refiner_json_retry")
    assert len(retries) == 1
    assert retries[0]["data"]["json_retry"] == 1


def test_all_attempts_unparseable_returns_unchanged_and_logs() -> None:
    logger = _LogRecorder()
    r = _refiner([_decode_error()] * (MAX_REFINER_JSON_RETRIES + 1), logger)

    out = r.refine(requirement="req", tasks=[TASK], last_coder_response="c",
                   last_result="res", exit_code=1, last_task_id="t1")

    # Behaviour preserved: caller gets the original list...
    assert [t["id"] for t in out] == ["t1"]
    # ...but the failure is now visible instead of looking like a no-op.
    assert len(r.coding_tool.prompts) == MAX_REFINER_JSON_RETRIES + 1
    assert len(logger.events_named("refiner_json_retry")) == MAX_REFINER_JSON_RETRIES
    assert logger.events_named("refine_exception")


def test_non_json_errors_are_not_retried() -> None:
    """Only parse failures get the JSON-only re-ask."""
    logger = _LogRecorder()
    r = _refiner([RuntimeError("provider died")], logger)

    out = r.refine(requirement="req", tasks=[TASK], last_coder_response="c",
                   last_result="res", exit_code=1, last_task_id="t1")

    assert [t["id"] for t in out] == ["t1"]
    assert len(r.coding_tool.prompts) == 1
    assert not logger.events_named("refiner_json_retry")
    assert logger.events_named("refine_exception")


def test_retry_budget_constant_is_sane() -> None:
    assert MAX_REFINER_JSON_RETRIES >= 1
