"""The refiner must accept a bare ARRAY of tasks (2026-10-23).

The incident
------------
On plan ``20261023-briefing-llm-resume`` the refiner's reply was a JSON
array of task objects rather than the ``{"tasks": [...]}`` object the
prompt asks for. ``Refiner.refine`` did::

    new_tasks = response.get("tasks", tasks)

which raised ``AttributeError: 'list' object has no attribute 'get'``.
The exception fell into the generic handler, the caller logged::

    refine_exception: Refiner failed for task [9]; returning the task
    list unchanged: 'list' object has no attribute 'get'

and the task list came back UNCHANGED -- so the task that had just been
split was never replaced by its children, and task 9 went straight to
``failed``. The reply was perfectly usable; only the shape was wrong.

What is pinned here
-------------------
* a bare array of task objects is accepted as the task list;
* the ``{"tasks": [...]}`` object keeps working exactly as before;
* a reply that is neither (a bare string, a number, ``None``, or an
  array whose entries are not task objects) does NOT raise
  ``AttributeError`` -- it takes the pre-existing JSON-retry path
  (re-ask for JSON only), and the run's failure path still reports it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def _refiner(responses: List[Any], logger: Optional[Any] = None) -> TaskRefiner:
    r = TaskRefiner.__new__(TaskRefiner)  # bypass AgentConfig wiring
    r.coding_tool = _StubCodingTool(responses)
    r.logger = logger
    r.config = type("C", (), {
        "refiner_system_prompt": "sys",
        "domain_knowledge": "",
    })()
    r.project_dir = None  # no validator -> returns the candidate directly
    return r


TASK = {"id": "t1", "title": "t", "description": "d", "status": "pending"}
CHILD_1 = {"id": "9-1", "title": "first", "description": "d", "status": "pending"}
CHILD_2 = {"id": "9-2", "title": "second", "description": "d", "status": "pending"}
PARENT = {"id": "9", "title": "p", "description": "d", "status": "pending"}


def _refine(r: TaskRefiner, tasks=None):
    return r.refine(
        requirement="req", tasks=tasks if tasks is not None else [TASK],
        last_coder_response="c", last_result="res", exit_code=1,
        last_task_id="t1",
    )


def test_bare_array_of_tasks_is_accepted_as_the_task_list() -> None:
    """The reply the model actually sent on the incident plan: a JSON
    array of task objects. It must be used, not rejected and not
    crashed on.
    """
    logger = _LogRecorder()
    r = _refiner([[CHILD_1, CHILD_2]], logger)

    out = _refine(r, tasks=[PARENT])

    assert [t["id"] for t in out] == ["9-1", "9-2"]
    # One ask: the array WAS the answer, so no re-ask was needed.
    assert len(r.coding_tool.prompts) == 1
    assert not logger.events_named("refiner_json_retry")
    assert not logger.events_named("refine_exception")


def test_tasks_object_behaviour_is_unchanged() -> None:
    """The shape the prompt asks for keeps working exactly as before."""
    logger = _LogRecorder()
    r = _refiner([{"tasks": [CHILD_1, CHILD_2]}], logger)

    out = _refine(r, tasks=[PARENT])

    assert [t["id"] for t in out] == ["9-1", "9-2"]
    assert len(r.coding_tool.prompts) == 1
    assert not logger.events_named("refiner_json_retry")


def test_a_non_list_reply_is_re_asked_not_crashed_on() -> None:
    """A bare string / number is JSON, but it is not a task list. It
    takes the SAME path an unparseable reply takes: re-ask for JSON
    only. The old code raised ``AttributeError`` here.
    """
    logger = _LogRecorder()
    r = _refiner(["not json", {"tasks": [CHILD_1]}], logger)

    out = _refine(r)

    # The re-ask recovered: the task list came back from the SECOND
    # reply, not from the first. "t1" survives because the second reply
    # is a delta (it does not mention "t1"); "9-1" is what it added.
    assert [t["id"] for t in out] == ["t1", "9-1"]
    assert len(r.coding_tool.prompts) == 2
    assert "SINGLE JSON object" in r.coding_tool.prompts[1]
    retries = logger.events_named("refiner_json_retry")
    assert len(retries) == 1
    assert retries[0]["data"]["json_retry"] == 1
    assert not logger.events_named("refine_exception")


def test_a_number_reply_is_re_asked_not_crashed_on() -> None:
    """Same as above, for a number: ``123.get(...)`` was an
    ``AttributeError`` too.
    """
    logger = _LogRecorder()
    r = _refiner([123, {"tasks": [CHILD_1]}], logger)

    out = _refine(r)

    assert [t["id"] for t in out] == ["t1", "9-1"]
    assert len(r.coding_tool.prompts) == 2
    assert len(logger.events_named("refiner_json_retry")) == 1
    assert not logger.events_named("refine_exception")


def test_an_array_of_non_tasks_is_re_asked_not_crashed_on() -> None:
    """An array whose entries are not task objects cannot be used: the
    downstream merge reads ``task.get("id")`` on every entry. It takes
    the JSON-retry path instead of raising ``AttributeError`` further
    down.
    """
    logger = _LogRecorder()
    r = _refiner([["not", "a", "task"], {"tasks": [CHILD_1]}], logger)

    out = _refine(r)

    assert [t["id"] for t in out] == ["t1", "9-1"]
    assert len(r.coding_tool.prompts) == 2
    assert len(logger.events_named("refiner_json_retry")) == 1
    assert not logger.events_named("refine_exception")


def test_a_persistently_wrong_shape_fails_the_way_an_unparseable_reply_does() -> None:
    """The budget is the pre-existing one, and the failure is reported
    the pre-existing way (``refine_exception`` + the task list
    unchanged) -- the crash it replaces used to look identical from the
    outside, but it was an ``AttributeError`` from a shape nobody
    handled.
    """
    logger = _LogRecorder()
    r = _refiner(["not json"] * (MAX_REFINER_JSON_RETRIES + 1), logger)

    out = _refine(r)

    assert [t["id"] for t in out] == ["t1"]
    assert len(r.coding_tool.prompts) == MAX_REFINER_JSON_RETRIES + 1
    assert len(logger.events_named("refiner_json_retry")) == MAX_REFINER_JSON_RETRIES
    assert logger.events_named("refine_exception")
