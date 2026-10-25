"""The HITL decision service: creating, listing and answering a decision.

Background
----------
A decision is asked by one actor and answered by another, so the answer
travels through the store rather than through memory.  What the *service*
adds on top of the store is the part that is not about SQL: which plan
configuration a new decision inherits, which states may still be
answered, and what a refusal leaves behind.

Three rules are pinned here, because each of them is a place where the
obvious implementation is wrong in a way nobody notices until a plan is
stuck waiting for a human who was never asked:

  1. **The plan's configuration is frozen into the row.**  ``mode`` and
     ``timeout_sec`` decide what happens when the deadline passes, so
     they are read from the plan's own ``hitl_config.json`` when the
     decision is created and stored with it.  Rewriting the file
     afterwards changes what the *next* decision inherits, never what an
     existing one is (``test_create_freezes_config``).
  2. **An ``auto`` decision without a default is refused.**  It would
     have to fall back to an answer that does not exist, and the refusal
     is written to the audit mirror so the question is not silently lost
     (``test_auto_requires_default``).
  3. **Only an open decision can be answered.**  ``pending`` and
     ``paused`` are open; ``answered`` / ``defaulted`` / ``unroutable``
     are settled and answer to them is a conflict, not a second answer
     (``test_answer_status_matrix``).

Why these live in an integration test
-------------------------------------
They cross three boundaries at once — the plan's configuration file, the
SQLite row and the audit mirror — and a unit test that stubbed any of
them away would pass while the real chain stayed broken.

Test command::

    pytest backend/tests/integration/test_hitl_service_create.py -v
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from config_paths import resolve_plans_dir, resolve_state_db_path
from hitl.models import Decision, DecisionMode, DecisionRequest, DecisionStatus
from hitl.service import (
    DecisionRejected,
    answer_decision,
    blocked_task_ids,
    create_decision,
    list_decisions,
)
from hitl.store import HitlStore
from hooks import write_for_plan
from utils.atomic_io import atomic_write_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(plan_id: str = "plan-hitl-service", **overrides: Any) -> DecisionRequest:
    """A valid request; ``overrides`` replaces single fields."""
    payload: Dict[str, Any] = {
        "plan_id": plan_id,
        "source": "execution",
        "task_id": "T-1",
        "question": "Retry the failing task?",
        "options": ["accept", "reject"],
        "default_option": "accept",
        "mode": DecisionMode.AUTO.value,
        "timeout_sec": 1800,
    }
    payload.update(overrides)
    return DecisionRequest(**payload)


def _audit(plan_id: str = "plan-hitl-service") -> List[Dict[str, Any]]:
    """The audit mirror of ``plan_id``, as a list of records."""
    path = resolve_plans_dir() / plan_id / "hitl_decisions.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _stored(store: HitlStore, decision_id: str) -> Decision:
    """The stored form of ``decision_id``."""
    decision = store.get(decision_id)
    assert decision is not None, f"decision {decision_id!r} is not stored"
    return decision


def _in_status(
    decision_id: str,
    status: str,
    plan_id: str = "plan-hitl-service",
) -> Decision:
    """A decision that sits in ``status`` before anybody answers it."""
    decision = Decision.from_request(_request(plan_id=plan_id), decision_id=decision_id)
    decision.status = status
    return decision


def _write_config(
    plan_dir: Path,
    *,
    mode: str = "auto",
    timeout_sec: int = 1800,
    hooks: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Write the plan's whole HITL configuration.

    Written by hand rather than through :func:`hooks.write_for_plan`,
    because that function only ever writes the hook list a plan's
    *origin* implies: a ``cc_connect`` destination, or none at all.
    The cases below need destinations the origin cannot express.

    The write is atomic, because the outlet reads this file from
    another thread while the plan is running: a torn read would look
    like a plan that names nobody.
    """
    atomic_write_json(
        plan_dir / "hitl_config.json",
        {
            "version": 1,
            "mode": mode,
            "timeout_sec": timeout_sec,
            "hooks": list(hooks or []),
        },
        reraise=True,
    )


def _write_hooks(plan_dir: Path, hooks: List[Dict[str, Any]]) -> None:
    """Set the plan's hook list — the destinations the outlet may try.

    Merged into whatever configuration the plan already has, so a test
    can give a plan an outlet without saying anything about its mode
    or its deadline.
    """
    path = plan_dir / "hitl_config.json"
    document: Dict[str, Any] = {}
    if path.exists():
        document = json.loads(path.read_text(encoding="utf-8"))
    document["hooks"] = list(hooks)
    atomic_write_json(path, document, reraise=True)


def _wait_for_status(
    store: HitlStore,
    decision_id: str,
    status: str,
    *,
    timeout: float = 10.0,
) -> Optional[str]:
    """Wait until ``decision_id`` reaches ``status``; return what it is.

    The delivery runs on its own thread, so the question is settled
    *after* the creation call has answered.  The wait is bounded, and
    the status it gives up with is the answer — a decision that never
    reaches the expected state is a failure, not a hang.
    """
    deadline = time.monotonic() + timeout
    current: Optional[str] = None
    while time.monotonic() < deadline:
        stored = store.get(decision_id)
        if stored is not None:
            current = stored.status
            if current == status:
                return current
        time.sleep(0.02)
    return current


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_id() -> str:
    return "plan-hitl-service"


@pytest.fixture
def plan_dir(plan_id: str) -> Path:
    """The plan's directory — where its ``hitl_config.json`` lives."""
    path = resolve_plans_dir() / plan_id
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def store() -> HitlStore:
    """A store on the per-test database, with the plans root as audit root."""
    opened = HitlStore(resolve_state_db_path())
    try:
        yield opened
    finally:
        opened.close()


# ---------------------------------------------------------------------------
# 1. The plan's configuration is frozen into the row
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_freezes_config(plan_dir: Path, plan_id: str, store: HitlStore) -> None:
    """Rewriting the plan's configuration does not touch an existing row.

    ``mode`` and ``timeout_sec`` decide what happens when the deadline
    passes, so they are read from the plan's own file *once* — when the
    decision is created — and stored with the decision.  A plan whose
    configuration is rewritten half-way through an execution must not
    change the meaning of the question it already asked.

    The ``hooks`` array is deliberately *not* frozen: it is the plan's
    event outlet, read at dispatch time, so the row must not carry it.
    The row has no field for it at all, which is what the last assertion
    pins.

    The plan is given an outlet, so the question is one somebody was
    asked — a plan with no outlet asks nobody, and its questions are
    ``unroutable`` from the start (see the last section of this file).
    """
    _write_config(
        plan_dir,
        mode="strict",
        timeout_sec=60,
        hooks=[{"kind": "cmd", "command": "true"}],
    )

    decision = create_decision(_request(plan_id=plan_id), plan_dir)

    assert decision.status == DecisionStatus.PENDING.value
    assert decision.mode == DecisionMode.STRICT.value
    assert decision.timeout_sec == 60
    assert decision.deadline_at is not None

    # The plan's configuration changes underneath the open decision.
    _write_config(
        plan_dir,
        mode="auto",
        timeout_sec=120,
        hooks=[{"kind": "cmd", "command": "true"}],
    )

    stored = _stored(store, decision.decision_id)
    assert stored.mode == DecisionMode.STRICT.value, (
        "the mode of an existing decision must stay frozen at the value it "
        f"was created with; got {stored.mode!r}"
    )
    assert stored.timeout_sec == 60, (
        "the timeout of an existing decision must stay frozen at the value "
        f"it was created with; got {stored.timeout_sec!r}"
    )
    assert stored.deadline_at == decision.deadline_at, (
        "the deadline of an existing decision must not move when the "
        "plan's configuration is rewritten"
    )
    assert "hooks" not in stored.to_row(), (
        "the hook list is the plan's event outlet, read when an event is "
        "dispatched; it must not be frozen into the decision row"
    )


# ---------------------------------------------------------------------------
# 2. A decision that could not be honoured is refused, not created
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_auto_requires_default(plan_dir: Path, plan_id: str, store: HitlStore) -> None:
    """An ``auto`` decision without a default option is refused.

    ``auto`` means "when the deadline passes, use the default answer".
    A decision with no default answer therefore has no way to end: it
    would have to fall back to nothing.  Such a decision is refused at
    creation time — half an hour before the deadline would have found
    the problem — and the refusal is written to the audit mirror,
    because a refused question leaves no row and would otherwise be
    invisible.
    """
    # No configuration file at all: the plan's effective mode is ``auto``.
    request = _request(plan_id=plan_id, default_option=None)

    with pytest.raises(DecisionRejected) as caught:
        create_decision(request, plan_dir)

    assert caught.value.reason == "auto_requires_default"
    assert list_decisions(plan_id) == [], (
        "a refused decision must not leave a row behind"
    )
    records = _audit(plan_id)
    assert [r["event"] for r in records] == ["rejected"], (
        "the refusal must be recorded in the audit mirror exactly once; "
        f"got {records!r}"
    )
    assert records[0]["note"] == "auto_requires_default"
    assert records[0]["plan_id"] == plan_id


@pytest.mark.integration
def test_strict_allows_missing_default(plan_dir: Path, plan_id: str) -> None:
    """A ``strict`` decision may have no default: it has no fallback to run.

    ``strict`` means "nobody answered, so nobody continues" — there is
    nothing for a default answer to do, so its absence is not a defect.
    """
    _write_config(
        plan_dir, mode="strict", hooks=[{"kind": "cmd", "command": "true"}]
    )

    decision = create_decision(
        _request(plan_id=plan_id, default_option=None), plan_dir
    )

    assert decision.mode == DecisionMode.STRICT.value
    assert decision.default_option is None
    assert decision.status == DecisionStatus.PENDING.value


@pytest.mark.integration
def test_model_rejects_negative_timeout(plan_id: str) -> None:
    """The model refuses a negative timeout before a service can see it.

    The model is the first line of defence; the service is the second
    (see the test below).  Both exist, and this pins which one catches
    which value.
    """
    with pytest.raises(ValueError):
        _request(plan_id=plan_id, timeout_sec=-1)


@pytest.mark.integration
@pytest.mark.parametrize("timeout_sec", [0, -1])
def test_create_rejects_non_positive_timeout(
    plan_dir: Path, plan_id: str, timeout_sec: int
) -> None:
    """A decision that is born expired is refused.

    ``timeout_sec <= 0`` means "the deadline is now", which is a
    decision nobody can answer in time — the request is nonsense and is
    refused rather than stored.

    A negative timeout is refused by the model before it can reach the
    service, so the request is mutated here to reach the service's own
    guard: the HTTP layer is that boundary, and a request that arrives
    there with a nonsense timeout must be refused rather than stored.
    """
    request = _request(plan_id=plan_id)
    request.timeout_sec = timeout_sec

    with pytest.raises(ValueError) as caught:
        create_decision(request, plan_dir)

    assert isinstance(caught.value, DecisionRejected)
    assert caught.value.reason == "invalid_timeout"
    assert list_decisions(plan_id) == []


@pytest.mark.integration
def test_create_rejects_empty_options(plan_dir: Path, plan_id: str) -> None:
    """A question with no options on the table cannot be answered.

    The model refuses an empty option set at construction; the service
    refuses it too, so the rule holds for a request that reached the
    service some other way.
    """
    request = _request(plan_id=plan_id)
    request.options = []

    with pytest.raises(ValueError) as caught:
        create_decision(request, plan_dir)

    assert isinstance(caught.value, DecisionRejected)
    assert caught.value.reason == "no_options"
    assert list_decisions(plan_id) == []


# ---------------------------------------------------------------------------
# 3. Only an open decision can be answered
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "status,expected_code",
    [
        (DecisionStatus.PENDING.value, 200),
        (DecisionStatus.PAUSED.value, 200),
        (DecisionStatus.ANSWERED.value, 409),
        (DecisionStatus.DEFAULTED.value, 409),
        (DecisionStatus.UNROUTABLE.value, 409),
    ],
)
def test_answer_status_matrix(
    store: HitlStore,
    plan_id: str,
    status: str,
    expected_code: int,
) -> None:
    """The answering status matrix, one row at a time.

    ``pending`` and ``paused`` are open — a paused decision is one whose
    strict deadline passed, and it stays answerable — so answering them
    succeeds.  ``answered`` / ``defaulted`` / ``unroutable`` are settled:
    a second answer to them is a conflict, and it must not overwrite
    what the first one stored.
    """
    decision_id = f"d-{status}"
    store.insert(_in_status(decision_id, status, plan_id=plan_id))

    result = answer_decision(decision_id, "accept", "alice", "")

    assert result[1] == expected_code, (
        f"answering a {status!r} decision must answer {expected_code}; "
        f"got {result!r}"
    )
    stored = _stored(store, decision_id)
    if expected_code == 200:
        assert result[0] == DecisionStatus.ANSWERED.value
        assert stored.status == DecisionStatus.ANSWERED.value
        assert stored.answer == "accept"
        assert stored.answered_by == "alice"
    else:
        assert result[0] == status
        assert stored.status == status, (
            f"a refused answer must leave the {status!r} decision as it was"
        )


@pytest.mark.integration
def test_answer_unknown_decision_is_404() -> None:
    """An answer to a decision that does not exist is a 404, not a 409.

    The two are different facts: 404 says "there is nothing here", 409
    says "there is something here and you may not change it".  A stale
    card pointing at a decision that was never created must not be told
    that the decision is in a state it cannot be in.
    """
    assert answer_decision("d-does-not-exist", "accept", "alice", "") == (
        "not_found",
        404,
    )


@pytest.mark.integration
def test_answer_twice_is_409(store: HitlStore, plan_id: str) -> None:
    """The second answer to a decision is a conflict and changes nothing.

    Two operators clicking "accept" at the same second must produce one
    stored answer; the loser is told the decision is already answered.
    """
    decision_id = "d-twice"
    store.insert(_in_status(decision_id, DecisionStatus.PENDING.value, plan_id=plan_id))

    first = answer_decision(decision_id, "accept", "alice", "")
    second = answer_decision(decision_id, "reject", "bob", "")

    assert first == (DecisionStatus.ANSWERED.value, 200)
    assert second == (DecisionStatus.ANSWERED.value, 409)
    stored = _stored(store, decision_id)
    assert stored.answer == "accept", (
        "the second answer must not overwrite the first one"
    )
    assert stored.answered_by == "alice"


# ---------------------------------------------------------------------------
# 4. Listing
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_list_decisions_filters_by_status(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """The list is the plan's decisions, optionally narrowed to one state.

    The ``pending`` list is what a plan's UI polls for; an unknown plan
    is not an error but an empty list, because the question a list
    answers is "what is outstanding", not "does the plan exist".
    """
    store.insert(_in_status("d-pending", DecisionStatus.PENDING.value, plan_id=plan_id))
    store.insert(_in_status("d-answered", DecisionStatus.ANSWERED.value, plan_id=plan_id))

    everything = list_decisions(plan_id)
    pending = list_decisions(plan_id, status=DecisionStatus.PENDING.value)

    assert {d.decision_id for d in everything} == {"d-pending", "d-answered"}
    assert {d.decision_id for d in pending} == {"d-pending"}
    assert list_decisions("no-such-plan") == []


@pytest.mark.integration
def test_create_then_list_round_trip(plan_dir: Path, plan_id: str) -> None:
    """A created decision is immediately visible in the plan's list."""
    _write_hooks(plan_dir, [{"kind": "cmd", "command": "true"}])

    decision = create_decision(_request(plan_id=plan_id), plan_dir)

    listed = list_decisions(plan_id)

    assert [d.decision_id for d in listed] == [decision.decision_id]
    assert listed[0].status == DecisionStatus.PENDING.value
    assert listed[0].question == "Retry the failing task?"


# ---------------------------------------------------------------------------
# 5. A question nobody was asked about
# ---------------------------------------------------------------------------
#
# A decision is only useful if somebody is asked.  The plan's hook list
# says who; a plan that names nobody — because it was created outside a
# session, or because every destination it names cannot be delivered to
# — has nobody to ask.  Such a question must not sit in the table as
# ``pending`` forever: it would hold the schedule shut (the task it is
# about and everything downstream of it) over a question no human was
# ever asked.  The state that means "nobody was asked" is ``unroutable``,
# and it settles the question without answering it.


@pytest.mark.integration
def test_no_outlet_makes_the_decision_unroutable(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """A plan with no hook list asks nobody, and the question says so.

    The plan's directory holds no ``hitl_config.json`` at all, so the
    plan says nothing about who to ask — the shape a plan created
    outside a session has.  The decision is written down (the audit
    mirror records that it was asked and that it went nowhere) and it
    is settled as ``unroutable``: not answered, not defaulted, and not
    waiting for a human who was never asked.
    """
    decision = create_decision(_request(plan_id=plan_id), plan_dir, store=store)

    assert decision.status == DecisionStatus.UNROUTABLE.value, (
        f"a decision nobody was asked about is {decision.status!r}; it "
        "must be unroutable"
    )
    stored = _stored(store, decision.decision_id)
    assert stored.status == DecisionStatus.UNROUTABLE.value
    assert stored.answer is None, (
        "a decision nobody was asked about was given an answer"
    )
    assert stored.answered_by is None
    assert stored.answered_at is None

    # The mirror records both facts: the question was asked, and it went
    # nowhere.  One line each — a second "unroutable" line would tell a
    # reader the question was settled twice.
    records = _audit(plan_id)
    assert [r["event"] for r in records] == ["created", "unroutable"], (
        f"the audit mirror does not record the transition: {records!r}"
    )
    assert records[-1]["status"] == DecisionStatus.UNROUTABLE.value

    # And the plan is not blocked by a question nobody knows about.
    assert blocked_task_ids(plan_id, store=store) == set(), (
        "a decision nobody was asked about still holds the schedule shut"
    )


@pytest.mark.integration
def test_every_destination_skipped_makes_the_decision_unroutable(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """A hook list whose every entry is skipped asks nobody either.

    An entry the outlet cannot deliver to — an unknown transport, a
    transport this build does not deliver on, a transport whose required
    field is missing — is skipped, and the question it was supposed to
    carry reaches nobody.  A list of nothing but skipped entries is the
    same situation as an empty list: nobody was asked.
    """
    _write_hooks(
        plan_dir,
        [{"kind": "no_such_transport", "destination": "nowhere"}],
    )

    decision = create_decision(_request(plan_id=plan_id), plan_dir, store=store)

    assert decision.status == DecisionStatus.UNROUTABLE.value, (
        f"a decision nobody could be asked is {decision.status!r}; it "
        "must be unroutable"
    )
    assert blocked_task_ids(plan_id, store=store) == set(), (
        "a decision nobody could be asked still holds the schedule shut"
    )

    # The outlet's own audit says where the event went: nowhere.
    hook_audit = plan_dir / "hooks_audit.jsonl"
    lines = [
        json.loads(line)
        for line in hook_audit.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 1, f"the outlet wrote {len(lines)} audit lines"
    assert lines[0]["delivered"] == 0
    assert lines[0]["skipped"] == 1
    assert lines[0]["entries"][0]["outcome"] == "skipped"


@pytest.mark.integration
def test_a_delivered_destination_keeps_the_decision_pending(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """A question somebody *was* asked stays open.

    The counterweight to the two cases above: the destination is
    deliverable and the delivery succeeds, so the question is still
    waiting for its human.  One broken destination does not degrade the
    whole question — that is the fan-out's promise.
    """
    _write_hooks(plan_dir, [{"kind": "cmd", "command": "true"}])

    decision = create_decision(_request(plan_id=plan_id), plan_dir, store=store)

    assert decision.status == DecisionStatus.PENDING.value, (
        f"a decision that was delivered is {decision.status!r}; it is "
        "still waiting for a human"
    )
    assert blocked_task_ids(plan_id, store=store) == {"T-1"}, (
        "an open question must hold the schedule shut"
    )


@pytest.mark.integration
def test_a_failing_destination_makes_the_decision_unroutable(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """A destination that was tried and refused leaves nobody to answer.

    The delivery runs on its own thread — a hung destination must not
    hold the request path — so the question is settled *after* the
    creation call has answered.  What the call returns is the question
    as it was committed; what the table holds a moment later is the
    truth: nobody was reached.
    """
    _write_hooks(plan_dir, [{"kind": "cmd", "command": "false"}])

    decision = create_decision(_request(plan_id=plan_id), plan_dir, store=store)

    status = _wait_for_status(
        store, decision.decision_id, DecisionStatus.UNROUTABLE.value
    )
    assert status == DecisionStatus.UNROUTABLE.value, (
        "a destination that refused the delivery leaves the question "
        f"open forever: {status!r}"
    )
    stored = _stored(store, decision.decision_id)
    assert stored.answer is None, (
        "a decision nobody was asked about was given an answer"
    )


@pytest.mark.integration
def test_creation_does_not_wait_for_a_hung_destination(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """A destination that hangs cannot hold the creation call hostage.

    The outlet's destinations each have their own clock, and the
    delivery runs off the request's thread: the caller is answered when
    the question is written down, not when the slowest destination
    answers.
    """
    _write_hooks(
        plan_dir,
        [{"kind": "cmd", "command": "sleep 5", "timeout_sec": 3}],
    )

    started = time.monotonic()
    decision = create_decision(_request(plan_id=plan_id), plan_dir, store=store)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, (
        f"the creation call took {elapsed:.3f}s; a hung destination "
        "delayed the caller"
    )
    assert decision.status == DecisionStatus.PENDING.value

    # The destination is abandoned on its own clock, and the question it
    # was supposed to carry reaches nobody: the question is settled
    # after the fact, not waited for.
    status = _wait_for_status(
        store, decision.decision_id, DecisionStatus.UNROUTABLE.value
    )
    assert status == DecisionStatus.UNROUTABLE.value, (
        "a hung destination leaves the question open forever: "
        f"{status!r}"
    )
