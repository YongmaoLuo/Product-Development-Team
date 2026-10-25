"""The refiner's step 0 — the necessity audit runs *before* the split.

The refiner used to go straight from "this task failed" to "split it".
Nothing in between asked whether the task was worth running at all,
and that is what pollutes the failure classification: a task that was
a duplicate, or whose acceptance could never be decided, produces a
failure the triage cannot tell apart from a genuine deliverable
defect — and it lands in the same "same cause twice" bucket as a real
one.

:mod:`refiner_necessity` answers the four questions (Q1 duplicate,
Q2 unverifiable acceptance, Q3 missing environment, Q4 colliding write
domain) as a pure function.  This module pins the wiring: the audit
runs as the **first** step of ``TaskRefiner.refine()``, its verdict
decides what the refiner is allowed to do, and its result rides back
with the round's result.

The contract pinned here:

* **Q1** — the duplicate is dropped and recorded; the model is never
  asked to split it;
* **Q2** — the split is refused and the reason comes back with the
  result.  The verdict is obeyed as it stands: there is no carve-out
  for a row the audit finds hard to judge, because a verdict that is
  recorded but not obeyed puts the failure straight back into the
  triage;
* **Q3** — the task is marked as an environment problem and is not
  split;
* **Q4** — the colliding tasks are serialized (``depends_on`` is
  chained) and the split goes on;
* a **failing audit** is recorded and the split continues — the audit
  must never be the reason a plan stops being refined;
* the task dicts that come back carry **the keys they carried
  before**: the audit travels beside the result, not inside the tasks.

Test command::

    cd backend && .venv/bin/python3 -m pytest tests/integration/test_refiner_necessity_step0.py -q
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import refiner as refiner_module  # noqa: E402
from refiner import TaskRefiner  # noqa: E402

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _LogRecorder:
    """Records the refiner's events, so a test can read the log."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def _record(self, level: str):
        def _fn(event: str, message: str, **kwargs: Any) -> None:
            self.events.append({
                "level": level,
                "event": event,
                "message": message,
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


class _StubCodingTool:
    """Scripted ``query_json``: every prompt it is asked is recorded."""

    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.prompts: List[str] = []

    def query_json(self, prompt, system_instruction=None, scene=None, **kwargs):
        self.prompts.append(prompt)
        item = self.responses.pop(0) if self.responses else {"tasks": []}
        if isinstance(item, Exception):
            raise item
        return item


def _refiner(
    responses: Optional[List[Any]] = None,
    logger: Optional[Any] = None,
) -> TaskRefiner:
    """A refiner with the model replaced by a script.

    ``project_dir`` stays ``None`` on purpose: without it the refiner
    has no output validator, so a round ends at the model's answer and
    the test sees the list the refiner actually decided on.
    """
    r = TaskRefiner.__new__(TaskRefiner)  # bypass the AgentConfig wiring
    r.coding_tool = _StubCodingTool(responses or [])
    r.logger = logger
    r.config = type("C", (), {
        "refiner_system_prompt": "sys",
        "domain_knowledge": "",
    })()
    r.project_dir = None
    return r


def _task(**overrides: Any) -> Dict[str, Any]:
    """One task row, with the fields the audit reads."""
    row = {
        "id": "1",
        "title": "A task",
        "description": "d",
        "status": "pending",
        "files_to_modify": ["src/a.py"],
        "test_command": "pytest -q tests/test_a.py",
        "depends_on": [],
    }
    row.update(overrides)
    return row


def _refine(
    refiner: TaskRefiner,
    tasks: List[Dict],
    last_task_id: str,
    **kwargs: Any,
):
    return refiner.refine(
        requirement="req",
        tasks=tasks,
        last_coder_response="c",
        last_result="res",
        exit_code=1,
        last_task_id=last_task_id,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Q1 — the duplicate is dropped before the split
# ---------------------------------------------------------------------------


def test_q1_dropped_before_split():
    """A duplicate of a task already in the batch is dropped, not split.

    The model is never asked: splitting a task that is not going to
    run produces children nobody asked for, and the failure it would
    produce would be filed as a deliverable defect.
    """
    first = _task(id="1", title="Implement the login flow")
    duplicate = _task(id="2", title="Implement the login flow")
    logger = _LogRecorder()
    r = _refiner(logger=logger)

    out = _refine(r, [first, duplicate], last_task_id="2")

    # The split was not called at all.
    assert r.coding_tool.prompts == []
    # The duplicate is gone; the task it duplicated stays.
    assert [t["id"] for t in out] == ["1"]
    # The drop is recorded, with the round's result.
    assert out.necessity_audit["dropped_duplicates"] == ["2"]
    assert out.necessity_audit["findings"], out.necessity_audit
    # ... and in the event log the refiner already writes to.
    logged = logger.events_named("refiner_necessity_audit")
    assert logged, logger.events
    assert logged[0]["data"]["dropped_duplicates"] == ["2"]


def test_q1_drops_a_duplicate_that_is_not_the_focus_and_still_splits():
    """A duplicate anywhere in the batch is dropped; the split goes on.

    The audit reads the whole batch, not just the task that failed, so
    a duplicate of an *earlier* task is dropped even when the failed
    task is a different one.
    """
    first = _task(id="1", title="Implement the login flow")
    duplicate = _task(id="2", title="Implement the login flow")
    other = _task(
        id="3",
        title="Something else",
        files_to_modify=["src/b.py"],
        test_command="pytest -q tests/test_b.py",
    )
    r = _refiner([{"tasks": [first, other]}])

    out = _refine(r, [first, duplicate, other], last_task_id="3")

    # The split happened for the task that failed...
    assert len(r.coding_tool.prompts) == 1
    # ...and the duplicate is gone from the result.
    assert "2" not in [t["id"] for t in out]
    assert out.necessity_audit["dropped_duplicates"] == ["2"]


# ---------------------------------------------------------------------------
# Q2 — the acceptance cannot be decided
# ---------------------------------------------------------------------------


def test_q2_rejects_split():
    """A task with no acceptance criterion is not split, and says why.

    Splitting it would produce children nobody can judge, and the
    failure would be filed as a deliverable defect — the exact
    pollution the audit exists to prevent.
    """
    unverifiable = _task(id="7", title="Do the thing", test_command="")
    logger = _LogRecorder()
    r = _refiner(logger=logger)

    out = _refine(r, [unverifiable], last_task_id="7")

    # The split was not called.
    assert r.coding_tool.prompts == []
    # The task is still there — a broken criterion is not a reason to
    # drop a task that is worth doing.
    assert [t["id"] for t in out] == ["7"]
    # The refusal and its reason come back with the result.
    reject = out.necessity_audit["reject_split"]
    assert reject is not None
    assert reject["task_id"] == "7"
    assert "acceptance" in reject["reason"], reject
    # ...and in the event log.
    logged = logger.events_named("refiner_necessity_audit")
    assert logged[0]["data"]["reject_split"]["task_id"] == "7"


def test_q2_rejects_a_row_that_declares_nothing():
    """A row with no acceptance criterion is refused, whatever it lacks.

    The Q2 verdict is obeyed as it stands: there is no carve-out for a
    row the audit finds hard to judge.  A verdict that is recorded but
    not obeyed would put the failure straight back into the triage —
    which is the thing this step exists to prevent — so a row that
    declares neither a test command nor a write domain is refused just
    like any other.
    """
    stub = {"id": "5", "title": "A task", "status": "pending"}
    r = _refiner([{"tasks": [stub]}])

    out = _refine(r, [stub], last_task_id="5")

    # The split was not called.
    assert r.coding_tool.prompts == []
    # The refusal and its reason come back with the result.
    reject = out.necessity_audit["reject_split"]
    assert reject is not None
    assert reject["task_id"] == "5"
    assert "acceptance" in reject["reason"], reject
    # The verdict is on the record too.
    rules = [f["rule"] for f in out.necessity_audit["findings"]]
    assert "Q2" in rules, out.necessity_audit


# ---------------------------------------------------------------------------
# Q3 — the environment the project does not have
# ---------------------------------------------------------------------------


def test_q3_marks_infra_without_splitting():
    """A task held for a missing environment is marked, not split.

    The failure would be environmental, so it must not be counted as
    the same cause twice — and the split would only reproduce it.
    """
    needs_port = _task(
        id="9",
        title="Wire the cache",
        files_to_modify=["src/cache.py"],
        test_command="pytest -q tests/test_cache.py",
        requires={"ports": ["6379"]},
    )
    r = _refiner()

    out = _refine(
        r,
        [needs_port],
        last_task_id="9",
        project_index={"ports": []},
    )

    # The split was not called.
    assert r.coding_tool.prompts == []
    # The task carries the environment marker.
    assert out[0]["failure_class"] == "infra"
    # ...and the reason is in the round's result.
    infra = out.necessity_audit["infra"]
    assert infra is not None
    assert infra["task_id"] == "9"
    assert "6379" in infra["reason"], infra


# ---------------------------------------------------------------------------
# Q4 — two tasks write the same bytes
# ---------------------------------------------------------------------------


def test_q4_serializes():
    """Two tasks claiming the same bytes are serialized, then split.

    The later task waits for the earlier one, so the two never write
    the same files at the same time.  The split itself still happens:
    the conflict is resolved, not a reason to stop.
    """
    earlier = _task(
        id="1",
        title="Write the parser",
        files_to_modify=["src/pkg"],
        test_command="pytest -q tests/test_parser.py",
    )
    later = _task(
        id="2",
        title="Write the parser helper",
        files_to_modify=["src/pkg/helper.py"],
        test_command="pytest -q tests/test_helper.py",
    )
    r = _refiner([{"tasks": [earlier, later]}])

    out = _refine(r, [earlier, later], last_task_id="2")

    # The split went on.
    assert len(r.coding_tool.prompts) == 1
    # The later task waits for the earlier one.
    by_id = {t["id"]: t for t in out}
    assert by_id["2"]["depends_on"] == ["1"]
    assert out.necessity_audit["serialized"] == {"2": ["1"]}


def test_q4_merged_domain_is_declared_before_the_split():
    """Both tasks of a colliding pair declare one merged write domain.

    Two different but overlapping declarations lock two different lock
    files, so each task would believe it holds the lock.  The merged
    declaration is what the model is shown, so the children it writes
    inherit it.
    """
    earlier = _task(
        id="1",
        title="Write the parser",
        files_to_modify=["src/pkg"],
        test_command="pytest -q tests/test_parser.py",
    )
    later = _task(
        id="2",
        title="Write the parser helper",
        files_to_modify=["src/pkg/helper.py"],
        test_command="pytest -q tests/test_helper.py",
    )
    r = _refiner([{"tasks": [earlier, later]}])

    _refine(r, [earlier, later], last_task_id="2")

    prompt = r.coding_tool.prompts[0]
    assert "src/pkg/helper.py" in prompt
    # The focus task goes out in full, and it carries the merged
    # declaration of the pair.
    assert prompt.count("src/pkg") >= 2, prompt


# ---------------------------------------------------------------------------
# The audit itself must never block the refinement
# ---------------------------------------------------------------------------


def test_audit_failure_does_not_block_the_split(monkeypatch):
    """A broken audit is recorded; the split still happens.

    The audit is a check on the plan, not the plan's engine.  Letting
    it stop a refinement would turn a broken rule into a broken plan.
    """
    def _boom(*args: Any, **kwargs: Any):
        raise RuntimeError("audit exploded")

    monkeypatch.setattr(refiner_module, "audit_tasks", _boom)

    task = _task(id="1")
    logger = _LogRecorder()
    r = _refiner([{"tasks": [task]}], logger=logger)

    out = _refine(r, [task], last_task_id="1")

    # The split happened.
    assert len(r.coding_tool.prompts) == 1
    # The failure is recorded, in the result and in the log.
    assert "audit exploded" in out.necessity_audit["error"]
    failed = logger.events_named("refiner_necessity_audit_failed")
    assert failed, logger.events
    assert "audit exploded" in failed[0]["data"]["error"]


# ---------------------------------------------------------------------------
# The shape of what comes back
# ---------------------------------------------------------------------------


def test_the_task_dicts_keep_the_keys_they_had():
    """The audit travels beside the result, not inside the task rows.

    The dispatcher decides whether the refiner changed anything by
    comparing the JSON of the two lists, so a key added to a task
    would make every round look like a change.
    """
    task = _task(id="1")
    before = sorted(task.keys())
    r = _refiner([{"tasks": [task]}])

    out = _refine(r, [task], last_task_id="1")

    assert sorted(out[0].keys()) == before
    # The caller's own dictionary is untouched as well.
    assert sorted(task.keys()) == before
    assert task["depends_on"] == []


def test_the_caller_survives_a_dropped_duplicate():
    """The caller keeps its own list — the refiner never writes to it.

    The dropped duplicate is gone from the *result*; the caller's list
    is not edited in place, because the caller compares it with the
    result to decide whether anything changed.
    """
    first = _task(id="1", title="Implement the login flow")
    duplicate = _task(id="2", title="Implement the login flow")
    tasks = [first, duplicate]
    snapshot = copy.deepcopy(tasks)
    r = _refiner()

    out = _refine(r, tasks, last_task_id="2")

    assert tasks == snapshot
    assert [t["id"] for t in out] == ["1"]
