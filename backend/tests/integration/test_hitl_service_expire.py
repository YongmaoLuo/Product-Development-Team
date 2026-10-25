"""The HITL decision service: expiry and the scheduling gate.

Background
----------
A decision is not a row that sits still.  Two things have to happen to
it, and both of them are decisions about *when* and about *what
blocks*:

  1. **Expiry.**  An ``auto`` decision whose deadline has passed
     resolves itself: the ``default_option`` becomes the answer and the
     decision ends ``defaulted``.  That has to happen **exactly once**
     — a second sweep must not append a second audit line for the
     same question — and an answer that arrived in the meantime has to
     win over the sweep rather than race it
     (``test_expire_idempotent``).
  2. **The scheduling gate.**  While a question is unanswered, the
     task it is about — and everything downstream of it — must not be
     scheduled.  The gate is *computed*, never stored: no task status
     changes, so answering the question opens the gate again by itself
     (``test_blocked_expands_downstream``).

Two kinds of decision deliberately do **not** resolve themselves.  A
decision nobody could be routed to (``unroutable``) has no answer to
fall back to, and a ``strict`` decision has no substitute for a human
— so neither of them may act in a human's place
(``test_unroutable_not_expired``).

Why these live in an integration test
-------------------------------------
Both actions cross the same three boundaries as the rest of the HITL
surface — the plan's configuration file, the SQLite row and the audit
mirror — and the interesting failures are exactly at those seams: a
sweep that writes the row but not the mirror, a gate that reads a
stale graph, an expiry that counts a row somebody else already
answered.

Time is injected, never slept
------------------------------
``expire_due(now=...)`` takes the clock as an argument, so the whole
expiry contract is asserted without a single ``sleep``.  The deadline
is derived from the same injected clock, so "one second past the
deadline" is an exact instant rather than a race — which is also what
keeps this file out of the ``time_sensitive`` lane.

Test command::

    pytest backend/tests/integration/test_hitl_service_expire.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytest

from config_paths import resolve_plans_dir, resolve_state_db_path
from hitl import service
from hitl.models import Decision, DecisionMode, DecisionRequest, DecisionStatus
from hitl.store import HitlStore
from hooks import config_path, write_for_plan
from utils.atomic_io import atomic_write_json

PLAN_ID = "plan-hitl-expire"

#: A destination that always succeeds, so the question is asked of
#: somebody.  The ``cmd`` transport runs the command and hands it the
#: payload on stdin; ``true`` exits 0 and touches nothing.  The
#: delivery runs on its own thread, so it never holds the decision's
#: creation path.
ASKABLE_HOOKS: List[Dict[str, Any]] = [{"kind": "cmd", "command": "true"}]

#: The instant every decision in this file is created at.  A fixed
#: clock is what makes "one second past the deadline" an exact
#: instant instead of a race against the wall clock.
NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(plan_id: str = PLAN_ID, **overrides: Any) -> DecisionRequest:
    """A valid request; ``overrides`` replaces single fields."""
    payload: Dict[str, Any] = {
        "plan_id": plan_id,
        "source": "execution",
        "task_id": "T-1",
        "question": "Retry the failing task?",
        "options": ["accept", "reject"],
        "default_option": "accept",
        "mode": DecisionMode.AUTO.value,
        "timeout_sec": 60,
    }
    payload.update(overrides)
    return DecisionRequest(**payload)


def _audit(plan_id: str = PLAN_ID) -> List[Dict[str, Any]]:
    """The audit mirror of ``plan_id``, as a list of records."""
    path = resolve_plans_dir() / plan_id / "hitl_decisions.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _events(plan_id: str, event: str) -> List[Dict[str, Any]]:
    """The audit records of ``plan_id`` carrying ``event``."""
    return [record for record in _audit(plan_id) if record.get("event") == event]


def _write_tasks(plan_id: str, tasks: List[Dict[str, Any]]) -> None:
    """Write the plan's static task graph into its ``tasks.json``."""
    plan_dir = resolve_plans_dir() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    document = {"requirement": "hitl expire fixture", "tasks": tasks}
    (plan_dir / "tasks.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


def _write_runtime_tasks(
    store: HitlStore, plan_id: str, tasks: List[Dict[str, Any]]
) -> None:
    """Write the plan's task graph into the ``plan_tasks`` mirror."""
    for task in tasks:
        store._conn.execute(  # noqa: SLF001 - the test writes the row the runtime owns
            "INSERT OR REPLACE INTO plan_tasks (plan_id, task_id, depends_on, "
            "updated_at) VALUES (?, ?, ?, ?)",
            (
                plan_id,
                task["id"],
                json.dumps(task.get("depends_on") or []),
                NOW.isoformat(),
            ),
        )
    store._conn.commit()


def _deadline_of(decision: Decision) -> datetime:
    """The decision's deadline, as an aware datetime."""
    assert decision.deadline_at, "the decision carries no deadline"
    return datetime.fromisoformat(decision.deadline_at)


def _past_deadline(decision: Decision) -> datetime:
    """One second past the decision's deadline."""
    return _deadline_of(decision) + timedelta(seconds=1)


def _stored(store: HitlStore, decision_id: str) -> Decision:
    """The stored form of ``decision_id``."""
    decision = store.get(decision_id)
    assert decision is not None, f"decision {decision_id!r} is not stored"
    return decision


def _write_config(plan_dir: Path, mode: str = "auto", timeout_sec: int = 60) -> None:
    """Write the plan's HITL configuration, with somebody to ask.

    The mode and the deadline are written by the product's own writer,
    so they are the ones the product would store.  The outlet is then
    given a destination the plan's *origin* cannot express:
    ``write_for_plan`` only ever writes the ``cc_connect`` hook a
    session implies, and a question nobody can be asked is
    ``unroutable`` from the start — there is nothing to wait for and
    nothing to expire.
    """
    document = write_for_plan(plan_dir, {"mode": mode, "timeout_sec": timeout_sec})
    document["hooks"] = list(ASKABLE_HOOKS)
    atomic_write_json(config_path(plan_dir), document, reraise=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_id() -> str:
    return PLAN_ID


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
# 1. Expiry happens once
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_expire_idempotent(plan_dir: Path, plan_id: str, store: HitlStore) -> None:
    """Three sweeps over the same expired decision move it exactly once.

    The contract has three parts, and each of them fails differently:

      * the **count** — the first sweep reports the one decision it
        moved, the two after it report nothing, because the row is no
        longer ``pending``;
      * the **row** — the decision ends ``defaulted`` and its answer is
        the ``default_option`` that was stored with it;
      * the **mirror** — the audit jsonl carries exactly one
        ``defaulted`` line.  A second line would mean a second reader
        downstream could see the same question resolved twice.
    """
    _write_config(plan_dir)
    created = service.create_decision(
        _request(plan_id=plan_id), plan_dir, store=store, now=NOW
    )
    expired_at = _past_deadline(created)

    assert service.expire_due(now=expired_at, store=store) == 1
    assert service.expire_due(now=expired_at, store=store) == 0
    assert service.expire_due(now=expired_at, store=store) == 0

    decision = _stored(store, created.decision_id)
    assert decision.status == DecisionStatus.DEFAULTED.value
    assert decision.answer == "accept"
    assert len(_events(plan_id, "defaulted")) == 1


@pytest.mark.integration
def test_expire_uses_the_stored_default_option(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """The answer of a defaulted decision is the option that was stored.

    Not "the first option", not "the option the caller passed to the
    sweep": the ``default_option`` frozen into the row when the
    question was asked.
    """
    _write_config(plan_dir)
    created = service.create_decision(
        _request(plan_id=plan_id, default_option="reject"),
        plan_dir,
        store=store,
        now=NOW,
    )

    assert service.expire_due(now=_past_deadline(created), store=store) == 1
    assert _stored(store, created.decision_id).answer == "reject"


@pytest.mark.integration
def test_expire_waits_for_the_deadline(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """A decision is resolved when its deadline arrives, not before.

    The deadline is the *end* of the waiting window, so the window is
    half-open: before the deadline the question is still open, and at
    the deadline it is late.  That is the same convention the rest of
    the backend uses for a freshness window — a thing is stale once its
    age reaches the window, not one tick before.
    """
    _write_config(plan_dir)
    created = service.create_decision(
        _request(plan_id=plan_id), plan_dir, store=store, now=NOW
    )

    assert service.expire_due(now=NOW, store=store) == 0
    assert (
        _stored(store, created.decision_id).status == DecisionStatus.PENDING.value
    )
    assert service.expire_due(now=_deadline_of(created), store=store) == 1
    assert (
        _stored(store, created.decision_id).status
        == DecisionStatus.DEFAULTED.value
    )


# ---------------------------------------------------------------------------
# 2. What expiry must NOT touch
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_unroutable_not_expired(plan_dir: Path, plan_id: str, store: HitlStore) -> None:
    """A decision nobody could be asked does not resolve itself.

    ``unroutable`` means there was nobody to answer: no hook, no
    channel, no human.  Falling back to the default option there would
    be the backend deciding a question it never asked anybody — the
    one thing the ``unroutable`` state exists to prevent.
    """
    write_for_plan(plan_dir, {"mode": "auto", "timeout_sec": 60})
    decision = Decision.from_request(
        _request(plan_id=plan_id), decision_id="d-unroutable", now=NOW
    )
    decision.status = DecisionStatus.UNROUTABLE.value
    store.insert(decision)

    assert service.expire_due(now=NOW + timedelta(days=1), store=store) == 0

    stored = _stored(store, "d-unroutable")
    assert stored.status == DecisionStatus.UNROUTABLE.value
    assert stored.answer is None
    assert _events(plan_id, "defaulted") == []


@pytest.mark.integration
def test_strict_decision_is_not_defaulted(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """A ``strict`` decision does not fall back when its deadline passes.

    ``strict`` means there is no substitute for a human, so the
    deadline does not resolve the question — it hands the decision to
    the verification side, which pauses the plan.  Defaulting it here
    would silently execute the default action a strict plan explicitly
    refused to allow.
    """
    _write_config(plan_dir, mode="strict")
    created = service.create_decision(
        _request(plan_id=plan_id, default_option=None),
        plan_dir,
        store=store,
        now=NOW,
    )
    assert created.mode == DecisionMode.STRICT.value

    assert service.expire_due(now=NOW + timedelta(days=1), store=store) == 0

    stored = _stored(store, created.decision_id)
    assert stored.status == DecisionStatus.PENDING.value
    assert stored.answer is None


@pytest.mark.integration
def test_answered_decision_is_not_defaulted(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """A question that was answered stays answered.

    The sweep only ever moves ``pending`` rows, so an answer that
    arrived first cannot be overwritten by a later sweep — the
    compare-and-set in the store decides, and the loser writes
    nothing.
    """
    _write_config(plan_dir)
    created = service.create_decision(
        _request(plan_id=plan_id), plan_dir, store=store, now=NOW
    )
    status, code = service.answer_decision(
        created.decision_id, "reject", store=store
    )
    assert (status, code) == (DecisionStatus.ANSWERED.value, 200)

    assert service.expire_due(now=NOW + timedelta(days=1), store=store) == 0

    stored = _stored(store, created.decision_id)
    assert stored.status == DecisionStatus.ANSWERED.value
    assert stored.answer == "reject"
    assert _events(plan_id, "defaulted") == []


@pytest.mark.integration
def test_expire_survives_an_unreadable_row(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """One row this module cannot read does not stop the sweep.

    A row written by another version of the backend can carry a value
    this module has to reject — a negative timeout, say.  The sweep
    still has to resolve the decisions it *can* read: otherwise one
    bad row would freeze every auto decision in every plan.
    """
    _write_config(plan_dir)
    created = service.create_decision(
        _request(plan_id=plan_id), plan_dir, store=store, now=NOW
    )
    store._conn.execute(  # noqa: SLF001 - the test writes the row another version would
        "INSERT INTO plan_decisions (decision_id, plan_id, source, question, "
        "options, mode, timeout_sec, status, default_option, deadline_at, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "d-foreign",
            plan_id,
            "execution",
            "Asked by another version?",
            json.dumps(["accept"]),
            DecisionMode.AUTO.value,
            -5,
            DecisionStatus.PENDING.value,
            "accept",
            (NOW - timedelta(seconds=1)).isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    store._conn.commit()

    assert service.expire_due(now=_past_deadline(created), store=store) == 1
    assert (
        _stored(store, created.decision_id).status
        == DecisionStatus.DEFAULTED.value
    )


# ---------------------------------------------------------------------------
# 3. The scheduling gate
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_blocked_expands_downstream(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """An open decision blocks its task and everything downstream of it.

    The graph is ``A → B → C → D`` and ``A → E``, with a pending
    decision on ``B`` and on ``C``.  So:

      * ``B`` and ``C`` are blocked — they carry the questions;
      * ``D`` is blocked — it depends on ``C``, transitively;
      * ``A`` and ``E`` are not — nothing they depend on is blocked.

    Answering ``B`` releases ``B`` itself; ``C`` still carries a
    question, so ``C`` and ``D`` stay blocked.  The gate is computed,
    so it opens by itself when the answer lands — no task status is
    touched, in either direction.
    """
    _write_tasks(
        plan_id,
        [
            {"id": "A", "depends_on": []},
            {"id": "B", "depends_on": ["A"]},
            {"id": "C", "depends_on": ["B"]},
            {"id": "D", "depends_on": ["C"]},
            {"id": "E", "depends_on": ["A"]},
        ],
    )
    _write_config(plan_dir)
    on_b = service.create_decision(
        _request(plan_id=plan_id, task_id="B"), plan_dir, store=store, now=NOW
    )
    on_c = service.create_decision(
        _request(plan_id=plan_id, task_id="C"), plan_dir, store=store, now=NOW
    )

    assert service.blocked_task_ids(plan_id, store=store) == {"B", "C", "D"}

    status, code = service.answer_decision(
        on_b.decision_id, "accept", store=store
    )
    assert (status, code) == (DecisionStatus.ANSWERED.value, 200)
    assert service.blocked_task_ids(plan_id, store=store) == {"C", "D"}

    status, code = service.answer_decision(
        on_c.decision_id, "accept", store=store
    )
    assert (status, code) == (DecisionStatus.ANSWERED.value, 200)
    assert service.blocked_task_ids(plan_id, store=store) == set()


@pytest.mark.integration
def test_paused_decision_blocks_downstream(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """A ``paused`` decision holds the gate shut just like a pending one.

    ``paused`` is what a ``strict`` decision becomes when its deadline
    passed: the plan stopped waiting, but the question was never
    resolved.  The task it guards — and its downstream — must stay out
    of the schedule until somebody answers.
    """
    _write_tasks(
        plan_id,
        [
            {"id": "A", "depends_on": []},
            {"id": "B", "depends_on": ["A"]},
            {"id": "C", "depends_on": ["B"]},
        ],
    )
    _write_config(plan_dir)
    created = service.create_decision(
        _request(plan_id=plan_id, task_id="B"), plan_dir, store=store, now=NOW
    )
    assert store.mark_paused(created.decision_id) is True

    assert service.blocked_task_ids(plan_id, store=store) == {"B", "C"}


@pytest.mark.integration
def test_settled_decisions_do_not_block(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """``answered`` and ``unroutable`` decisions block nothing.

    A settled question is not an open one: the work it was about may
    go on.  ``unroutable`` in particular must not block the plan —
    there is nobody to answer it, so blocking would strand the plan
    forever.
    """
    _write_tasks(
        plan_id,
        [
            {"id": "A", "depends_on": []},
            {"id": "B", "depends_on": ["A"]},
            {"id": "C", "depends_on": ["B"]},
        ],
    )
    _write_config(plan_dir)

    answered = service.create_decision(
        _request(plan_id=plan_id, task_id="B"), plan_dir, store=store, now=NOW
    )
    service.answer_decision(answered.decision_id, "accept", store=store)

    unroutable = Decision.from_request(
        _request(plan_id=plan_id, task_id="C"),
        decision_id="d-unroutable",
        now=NOW,
    )
    unroutable.status = DecisionStatus.UNROUTABLE.value
    store.insert(unroutable)

    assert service.blocked_task_ids(plan_id, store=store) == set()


@pytest.mark.integration
def test_gate_reads_the_runtime_task_graph(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """The gate reads the graph from the runtime mirror as well.

    ``plan_tasks`` is where the dispatcher keeps the static fields of
    every task, and it is written by paths that never touch
    ``tasks.json`` (the refiner, the verification repair).  A gate
    that only read the file would miss a graph that only exists in the
    database.
    """
    _write_runtime_tasks(
        store,
        plan_id,
        [
            {"id": "A", "depends_on": []},
            {"id": "B", "depends_on": ["A"]},
            {"id": "C", "depends_on": ["B"]},
        ],
    )
    _write_config(plan_dir)
    service.create_decision(
        _request(plan_id=plan_id, task_id="B"), plan_dir, store=store, now=NOW
    )

    assert service.blocked_task_ids(plan_id, store=store) == {"B", "C"}


@pytest.mark.integration
def test_gate_without_decisions_is_empty(
    plan_dir: Path, plan_id: str, store: HitlStore
) -> None:
    """A plan with no decisions schedules everything.

    The gate is the *only* thing that holds tasks back, so an empty
    decision list has to leave the plan's schedule untouched.
    """
    _write_tasks(
        plan_id,
        [
            {"id": "A", "depends_on": []},
            {"id": "B", "depends_on": ["A"]},
        ],
    )
    assert service.blocked_task_ids(plan_id, store=store) == set()