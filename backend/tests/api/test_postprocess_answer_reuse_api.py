"""The finishing stage's third action: answering a human wait.

What is pinned here
-------------------
The operator panel's waiting rows carry three actions: **extend** the
deadline, **terminate** the wait, and **answer** the question.  The
first two are the finishing stage's own endpoints; the third one is not
— answering a question is what the existing HITL service already does,
so the finishing stage **reuses** that path rather than growing a second
answer channel of its own (architecture decision point 6).

Reuse is a contract, and this file is the ruler for it:

* **the answer is a compare-and-set.**  The first answer wins; a second
  one is a conflict (409), never an overwrite.  Two operators clicking
  "accept" in the same second produce one stored answer;
* **the answer is scoped to its plan.**  A decision of another plan is
  not found (404), so a stale card cannot answer somebody else's
  question;
* **a settled question refuses an answer.**  Giving up on a wait
  settles the question it asked, so a late answer is a conflict (409)
  as well;
* **an answered wait leaves the panel.**  The waiting row is what says
  "this is waiting"; once the answer landed the row is gone, and the
  plan is no longer parked awaiting an answer — so the next scheduler
  tick may take it back.

The requests are real HTTP requests
-----------------------------------
The cases drive the **mounted app**, never the route functions: the
answer an operator gets is the answer the HTTP layer gives, status code
and body together.  A case that called the function directly would
prove the body and not the status, and the status is half of what this
contract is.

The waits are made by the production registry
---------------------------------------------
A wait is created by :class:`postprocess.waits.WaitRegistry`, which
writes it into the plan's own progress file and — bridged onto the
HITL store — asks the question through the very table the answer
endpoint reads.  The cases create their waits the same way, so what the
endpoint reads is a record the production code wrote, not a hand-made
file that happens to look like one.

Who settles the wait
--------------------
Two stores are involved and they are not the same one: the **decision
row** belongs to the existing HITL table, and the **wait record**
belongs to the finishing stage's progress file.  An answer that arrives
over HTTP settles the first; the finishing stage settles the second
with :meth:`WaitRegistry.resolve_human_wait`, which *adopts* an answer
somebody else already landed instead of writing it twice.  The cases
drive both halves, because both halves are part of the contract: the
answer lands, and then the waiting row is gone.

The OpenAPI snapshot
--------------------
The finishing stage's endpoints are published, so the snapshot of the
HTTP contract grows with them.  The snapshot may only ever **grow**:
a new path and a new field are fine, a changed field type is contract
drift, and drift is what this file refuses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from config_paths import resolve_plans_dir, resolve_state_db_path  # noqa: E402
from hitl.store import HitlStore  # noqa: E402
from plan_scheduler.facts import PlanFactsReader  # noqa: E402
from postprocess.progress import load_progress  # noqa: E402
from postprocess.waits import WaitRegistry  # noqa: E402
from routes.scheduler import plan_waiting_rows  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402


client = TestClient(server.app)

#: The plan the cases act on.
PLAN_ID = "answer-reuse-plan"

#: Another plan.  A decision of this one may not be answered through
#: the first plan's path.
OTHER_PLAN_ID = "answer-reuse-other-plan"

#: The requirement item that waits.
ITEM_ID = "approve"

#: The option the human picks.
OPTION = "proceed"

#: The published HTTP contract — the snapshot this file pins.
SNAPSHOT_PATH = Path(__file__).resolve().parent / "openapi_snapshot.json"


# ---------------------------------------------------------------------------
# The fixtures the cases act through
# ---------------------------------------------------------------------------


class _Waiting:
    """One plan that waits for a human, and the pieces that made it wait.

    The wait record lives in the finishing stage's progress file and is
    owned by the registry; the question lives in the HITL table and is
    owned by the store.  Both are here because the contract spans both.
    """

    def __init__(
        self,
        plan_id: str,
        item_id: str,
        decision_id: str,
        store: HitlStore,
        registry: WaitRegistry,
    ) -> None:
        self.plan_id = plan_id
        self.item_id = item_id
        self.decision_id = decision_id
        self.store = store
        self.registry = registry

    def close(self) -> None:
        """Close the store the case opened."""
        self.store.close()


def _human_item(item_id: str) -> Dict[str, Any]:
    """A requirement item that waits for a human."""
    return {"id": item_id, "wait": {"type": "human"}}


def _ask(
    plan_id: str, item_id: str, store: HitlStore
) -> Tuple[WaitRegistry, Dict[str, Any]]:
    """Ask one plan a question, through the production registry.

    The registry is the only thing that may create a wait, and it writes
    the record into the plan's own progress file — which is where the
    panel reads it from.  Bridged onto the store, it also stores the
    question in the HITL table, which is where the answer endpoint reads
    it from.

    Returns the registry and the wait's record, because both are needed:
    the registry is what settles the wait once the answer has landed, and
    the record carries the id of the row the question was stored as.
    """
    registry = WaitRegistry(plan_id, store=store)
    record = registry.create_human_wait(
        _human_item(item_id), question="批准合入？"
    )
    return registry, record


@pytest.fixture
def waiting(isolated_plans_dir):
    """A plan that waits for a human, asked through the production pieces."""
    store = HitlStore(resolve_state_db_path())
    registry, record = _ask(PLAN_ID, ITEM_ID, store)
    try:
        yield _Waiting(PLAN_ID, ITEM_ID, record["decision_id"], store, registry)
    finally:
        store.close()


def answer_url(plan_id: str, decision_id: str) -> str:
    """The answer endpoint of one decision."""
    return f"/api/hitl/{plan_id}/decisions/{decision_id}/answer"


def terminate_url(plan_id: str, item_id: str = ITEM_ID) -> str:
    """The terminate endpoint of one waiting item."""
    return f"/api/scheduler/{plan_id}/waits/{item_id}/terminate"


def answer(plan_id: str, decision_id: str, **body: Any):
    """``POST /api/hitl/{plan_id}/decisions/{decision_id}/answer``.

    The body carries ``option`` — the field the existing endpoint has
    always taken.  The finishing stage reuses that endpoint whole, so it
    reuses that body whole too; it does not introduce a second spelling
    for "the answer".
    """
    payload: Dict[str, Any] = {"option": OPTION, "answered_by": "operator"}
    payload.update(body)
    return client.post(answer_url(plan_id, decision_id), json=payload)


def is_parked(plan_id: str) -> bool:
    """Whether the scheduler still reads this plan as awaiting an answer.

    This is the very question the scheduler's tick asks, through the
    very read model it asks it with: a plan with a pending decision is
    parked, and a parked plan is not admitted.
    """
    connection = open_db(resolve_state_db_path())
    try:
        facts = PlanFactsReader(connection, resolve_plans_dir())
        return facts.has_pending_decision(plan_id)
    finally:
        connection.close()


def rows(plan_id: str) -> List[Dict[str, Any]]:
    """The panel's waiting rows for one plan."""
    return plan_waiting_rows(plan_id)


# ---------------------------------------------------------------------------
# 1 — the answer, and the row it takes off the panel
# ---------------------------------------------------------------------------


def test_answer_resumes_waiting_item(waiting):
    """An answer settles the wait, and the waiting row is gone.

    Three facts, checked apart, because they fail differently:

    * the answer **lands** — the HTTP contract answers 200 and the
      decision row reads ``answered``;
    * the **wait follows** — the finishing stage settles the wait with
      the answer that landed, and the durable record says the wait was
      answered;
    * the **panel follows** — the waiting row is gone, and the plan is
      no longer parked awaiting an answer, so the next tick may take it
      back.
    """
    plan_id = waiting.plan_id

    assert len(rows(plan_id)) == 1, (
        "the plan is waiting before the answer — a case that found no "
        "waiting row would prove nothing about the row that disappears"
    )
    assert rows(plan_id)[0]["item_id"] == ITEM_ID
    assert is_parked(plan_id) is True, (
        "a plan with a pending question is parked; the scheduler reads "
        "it as awaiting an answer"
    )

    response = answer(plan_id, waiting.decision_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision_id"] == waiting.decision_id
    assert body["status"] == "answered", (
        "the answer landed: the decision reads answered"
    )
    assert body["answer"] == OPTION

    # The finishing stage settles the wait with the answer that landed.
    settled = waiting.registry.resolve_human_wait(ITEM_ID, OPTION)
    assert settled["status"] == "answered", (
        "the wait follows the decision: the question was answered, so the "
        "wait is over"
    )
    assert load_progress(plan_id)["items"][ITEM_ID]["wait"]["status"] == (
        "answered"
    ), "the durable record says the wait was answered"

    assert rows(plan_id) == [], (
        "an answered wait is not waiting any more, so it has no row"
    )
    assert is_parked(plan_id) is False, (
        "the plan is no longer parked awaiting an answer, so the next "
        "tick may take it back"
    )


def test_answer_from_other_plan_returns_404(waiting):
    """A decision of another plan is not found.

    The answer is scoped to the plan in the path.  A stale card — one
    that names a decision of a plan the reader is not looking at — must
    not be able to answer somebody else's question, so the decision is
    reported as not found rather than as a conflict.
    """
    _, other = _ask(OTHER_PLAN_ID, ITEM_ID, waiting.store)
    decision_id = other["decision_id"]

    response = answer(PLAN_ID, decision_id)

    assert response.status_code == 404, (
        f"a decision of another plan is not found, got "
        f"{response.status_code}: {response.text}"
    )
    assert waiting.store.get(decision_id).status == "pending", (
        "the refused answer changed nothing: the question is still open"
    )


def test_second_answer_returns_409(waiting):
    """The second answer is a conflict, and it changes nothing.

    The first answer wins.  Two operators clicking "accept" in the same
    second must produce one stored answer, and the loser must be told
    that the decision is already settled — a second answer is a
    conflict, never an overwrite.
    """
    first = answer(PLAN_ID, waiting.decision_id)
    assert first.status_code == 200, first.text

    second = answer(PLAN_ID, waiting.decision_id)

    assert second.status_code == 409, (
        f"a second answer is a conflict, got {second.status_code}: "
        f"{second.text}"
    )
    assert second.json()["status"] == "answered", (
        "the conflict names the status the decision is actually in"
    )
    assert waiting.store.get(waiting.decision_id).answer == OPTION, (
        "the first answer is the one that stands — a second answer never "
        "overwrites it"
    )


def test_answer_after_terminate_returns_409(waiting):
    """A wait that was given up on refuses a late answer.

    Giving up on a wait settles the question it asked: the question was
    asked and nobody answered it, so a late answer is a conflict rather
    than an answer that lands as if it were asked for.
    """
    terminated = client.post(terminate_url(PLAN_ID))
    assert terminated.status_code == 200, terminated.text

    response = answer(PLAN_ID, waiting.decision_id)

    assert response.status_code == 409, (
        f"an answer to a settled question is a conflict, got "
        f"{response.status_code}: {response.text}"
    )
    assert waiting.store.get(waiting.decision_id).answer is None, (
        "the refused answer stored nothing: the question was settled "
        "without an answer"
    )


# ---------------------------------------------------------------------------
# 2 — the published contract
# ---------------------------------------------------------------------------


#: The paths the finishing stage added to the HTTP surface, and the
#: method each of them answers with.
FINISHING_STAGE_PATHS = (
    ("/api/scheduler/{plan_id}/waits/{item_id}/extend", "post"),
    ("/api/scheduler/{plan_id}/waits/{item_id}/terminate", "post"),
    ("/api/scheduler/{plan_id}/waits/{item_id}/deadline", "patch"),
    ("/api/scheduler/{plan_id}/postprocess-spec", "get"),
    ("/api/scheduler/{plan_id}/postprocess-spec", "put"),
)

#: The path the finishing stage **reuses** — the existing HITL answer
#: endpoint, which is why the answer needs no path of its own.
REUSED_ANSWER_PATH = "/api/hitl/{plan_id}/decisions/{decision_id}/answer"

#: The request models the finishing stage added.
FINISHING_STAGE_MODELS = ("ExtendWaitRequest", "SetDeadlineRequest")

#: The field types of every model that existed before the finishing
#: stage's endpoints were added.  They are written out here rather than
#: read from the snapshot, because the snapshot is the artefact under
#: test: a table that read itself would prove nothing.
#:
#: The spelling of a type is the one this file defines in
#: :func:`_type_of` — ``string``, ``integer``, ``boolean``, ``object``,
#: ``array<...>`` for what an array holds, ``union(...)`` for a field
#: that may be one of several types, the name of a model a field refers
#: to, and ``None`` for a field that declares no type at all.
LEGACY_FIELD_TYPES: Dict[str, Dict[str, Optional[str]]] = {
    "AddDecisionPointRequest": {
        "count": "integer",
        "requirement": "string",
    },
    "AnswerDecisionRequest": {
        "answered_by": "union(null,string)",
        "note": "string",
        "option": "string",
    },
    "AnswerInterviewRequest": {
        "answer": "string",
        "dimension": "string",
    },
    "CapacityRequest": {
        "reset": "boolean",
        "total": "union(integer,null)",
    },
    "ContinueInterviewRequest": {
        "reply": "string",
    },
    "CreateDecisionRequest": {
        "answer_note": "union(null,string)",
        "default_option": "union(null,string)",
        "failure_class": "union(null,string)",
        "mode": "union(null,string)",
        "options": "array<string>",
        "question": "string",
        "round": "union(integer,null)",
        "source": "string",
        "task_id": "union(null,string)",
        "timeout_sec": "union(integer,null)",
        "vp_id": "union(null,string)",
    },
    "GeneratePRDRequest": {
        "project_dir": "union(null,string)",
    },
    "HTTPValidationError": {
        "detail": "array<ValidationError>",
    },
    "ImportPlanRequest": {
        # `allow_duplicate` 是 2026-11-20 给这个请求体加的默认 False 布尔：
        # 重复导入默认被 409 拒绝，只有显式置 True 才放行。它与 `mode` 一样
        # 是**已批准的契约增长**，原有字段的名字与类型全部未变。
        "allow_duplicate": "boolean",
        "content": None,
        "hook_destination": "union(null,string)",
        "hook_session_id": "union(null,string)",
        # `mode` was added to this request later, as a non-optional
        # string carrying a default — so the published shape has no
        # null branch for it.  It is recorded here as an approved growth
        # of the contract; the fields that were already here kept both
        # their names and their types.
        "mode": "string",
        "priority": "integer",
        "project_dir": "union(null,string)",
        "raw": None,
        "requirement": None,
        "tasks": None,
        "title": "union(null,string)",
    },
    "PlanPhaseTransitionRequest": {
        "target_phase": "union(null,string)",
    },
    "PlanStateUpdateRequest": {
        "arch_enabled": "union(boolean,null)",
        "current_phase": "union(null,string)",
        "test_enabled": "union(boolean,null)",
    },
    "RefineRequest": {
        "feedback": "union(null,string)",
    },
    "ResetRoundsRequest": {
        "new_max_rounds": "union(integer,null)",
        "reset_round_to": "union(integer,null)",
    },
    "ResetVerificationRequest": {
        "clear_artifacts": "boolean",
        "clear_root_cause_chain": "boolean",
        "clear_verification_plan": "boolean",
        "force": "boolean",
        "restart_at_round": "integer",
        "supersede_repair_tasks": "boolean",
    },
    "ReviewActionRequest": {
        "action": "string",
        "note": "string",
        "question": "string",
    },
    "RevisePlanRequest": {
        "content": None,
        "feedback": "union(null,string)",
    },
    "StartExecutionRequest": {
        "project_dir": "union(null,string)",
        "sync_targets": "union(array<string>,null)",
        "tool": "union(null,string)",
    },
    "StartInterviewRequest": {
        "hook_destination": "union(null,string)",
        "hook_session_id": "union(null,string)",
        "plan_id": "union(null,string)",
        "requirement": "string",
    },
    "StartPlanRequest": {
        "force": "boolean",
        "max_rounds": "union(integer,null)",
        "project_dir": "union(null,string)",
        "sync_targets": "union(array<string>,null)",
        "tool": "union(null,string)",
    },
    "StartVerificationRequest": {
        "auto_fix": "union(boolean,null)",
        "force": "union(boolean,null)",
        "max_parallel": "union(integer,null)",
        "max_rounds": "union(integer,null)",
        "tool": "union(null,string)",
    },
    "StopPlanRequest": {
        "requeue": "boolean",
    },
    "TaskDeltaRequest": {
        "actor": "string",
        "add": "array<object>",
        "obsolete": "array<object>",
        "round": "integer",
    },
    "ValidationError": {
        "ctx": "object",
        "input": None,
        "loc": "array<union(integer,string)>",
        "msg": "string",
        "type": "string",
    },
    "_ExecutionStatusResponse": {
        "data_source": "string",
        "data_source_detail": "union(null,object)",
        "ended_at": "union(null,string)",
        "interrupted_task_count": "integer",
        "logs": "array<string>",
        "paused": "boolean",
        "paused_at": "union(null,string)",
        "pid": "union(integer,null)",
        "plan_id": "string",
        "project_dir": "string",
        "started_at": "union(null,string)",
        "status": "string",
        "stop_reason": "union(null,string)",
        "sync_targets": "union(array<string>,null)",
    },
    "_VerificationPlanReviewActionRequest": {
        "action": "string",
        "doc_sha256": "union(null,string)",
        "note": "string",
    },
}


def _type_of(prop: Any) -> Optional[str]:
    """The type of one property, spelled the way this file spells them.

    A property that names a model is that model's name; a nullable one
    is the union of what it may be; an array is what it holds.  A
    property that declares no type at all is ``None`` — "anything",
    which is a type of its own and must not be read as "a string".
    """
    if not isinstance(prop, dict):
        return None
    if "$ref" in prop:
        return str(prop["$ref"]).rsplit("/", 1)[-1]
    if "anyOf" in prop:
        members = sorted(str(_type_of(member)) for member in prop["anyOf"])
        return "union(" + ",".join(members) + ")"
    kind = prop.get("type")
    if isinstance(kind, list):
        # A union written as a list of types rather than as ``anyOf``.
        members = sorted(str(member) for member in kind)
        return "union(" + ",".join(members) + ")"
    if kind == "array":
        return "array<%s>" % _type_of(prop.get("items") or {})
    if kind is not None:
        return str(kind)
    return None


def _field_types(schemas: Dict[str, Any], names: Any) -> Dict[str, Any]:
    """The field types of the named models, as they are in the snapshot."""
    types: Dict[str, Any] = {}
    for name in names:
        model = schemas.get(name)
        if model is None:
            types[name] = None
            continue
        properties = model.get("properties") or {}
        types[name] = {
            key: _type_of(value) for key, value in sorted(properties.items())
        }
    return types


def test_openapi_snapshot_contains_new_paths():
    """The snapshot publishes the new paths, and the old contract is intact.

    Three things are checked, and they are checked apart:

    * the **new paths** are there — the finishing stage's three wait
      actions and the requirement book, with the methods they answer
      with;
    * the **reused path** is there — the answer the finishing stage
      reuses is published under its own name, so a reader of the
      contract can find it;
    * the **old field types** are unchanged — every request model that
      existed before the finishing stage's endpoints still carries the
      fields it carried, with the types it carried.  A snapshot that
      only ever grows is the contract; a snapshot that changes a field's
      type is drift, and drift is refused.
    """
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    paths = snapshot.get("paths") or {}
    schemas = (snapshot.get("components") or {}).get("schemas") or {}

    for path, method in FINISHING_STAGE_PATHS:
        assert path in paths, (
            f"the snapshot does not publish {path!r} — the endpoint the "
            "finishing stage added is not part of the published contract"
        )
        assert method in paths[path], (
            f"{path!r} is published without the {method!r} method it "
            "answers with"
        )

    assert REUSED_ANSWER_PATH in paths, (
        "the answer endpoint the finishing stage reuses is not published"
    )
    assert "post" in paths[REUSED_ANSWER_PATH], (
        "the reused answer endpoint is published without the method it "
        "answers with"
    )

    for model in FINISHING_STAGE_MODELS:
        assert model in schemas, (
            f"the request model {model!r} is not part of the published "
            "contract"
        )

    legacy = _field_types(schemas, LEGACY_FIELD_TYPES)
    assert legacy == LEGACY_FIELD_TYPES, (
        "a pre-existing request model changed its fields or their types; "
        "the snapshot may only grow:\n"
        + json.dumps(legacy, indent=2, sort_keys=True, ensure_ascii=False)
    )
