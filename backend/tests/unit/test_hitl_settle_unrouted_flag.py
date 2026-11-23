"""A failure-review question may stay ``pending`` so a human can answer it.

Background
----------
``create_decision`` settles a question nobody was asked as ``unroutable``:
a plan whose outlet names nobody cannot be asked, so the question is
closed on the spot and holds nothing up.  That is right for every caller
so far.

It is wrong for the failure-review caller.  The question is meant to be
answered on the panel, and the panel answers it out of the store — but a
plan with no ``hitl_config.json`` binding has no outlet, so the question
was ``unroutable`` from the start, and the answer API refuses a settled
decision with ``409``.  The question could never be answered at all.

So ``create_decision`` grows one keyword argument, ``settle_unrouted``,
defaulting to ``True``: every existing caller keeps the behaviour it has
today.  ``settle_unrouted=False`` leaves the question ``pending`` on both
settlement paths — the plan names nobody *and* the outlet accepted nobody
— so the question stays answerable.

What this pins
--------------
1. **The default still settles.**  A plan with no configuration at all is
   ``unroutable`` — the callers that existed before this argument see no
   change.
2. **The flag keeps the question open.**  The same plan, the same
   question, with ``settle_unrouted=False``: ``pending``.
3. **A pending question can be answered.**  The answer API takes it and
   answers it (``200``) instead of refusing it as already settled
   (``409``).
4. **The delivery is unchanged.**  The dispatch still happens and is still
   recorded in the audit mirror: a plan with no binding only ever logs,
   it never raises, and the flag is about the *settlement*, not about the
   delivery.
5. **The second settlement path is covered too.**  A plan that *does*
   name a destination, whose delivery reaches nobody, settles on the
   worker thread — and the flag has to hold there as well, otherwise the
   question would still be closed the moment the delivery finishes.

Test command::

    pytest backend/tests/unit/test_hitl_settle_unrouted_flag.py -v
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from config_paths import resolve_state_db_path
from hitl.models import DecisionRequest, DecisionStatus
from hitl.service import EVENT_DECISION_CREATED, answer_decision, create_decision
from hitl.store import HitlStore
from hooks.dispatcher import DispatchReport
from utils.atomic_io import atomic_write_json


#: The plan the questions are asked of.
PLAN_ID = "p1"

#: How long the worker thread gets to settle a question it was told to
#: leave alone.  The grace period is generous on purpose: it only has to
#: outlast the handful of instructions between "the delivery finished"
#: and "the row was rewritten".
GRACE_SEC = 5.0

#: The pause after the delivery has been attempted, before the status is
#: read.  A settlement that was going to happen happens in the
#: microseconds after the delivery returns; this is the headroom.
SETTLE_GRACE_SEC = 0.3


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(isolated_plans_dir) -> Path:
    """A plan with no ``hitl_config.json`` — nobody can be asked."""
    path = Path(isolated_plans_dir) / PLAN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def store() -> HitlStore:
    """A store on the per-test database."""
    opened = HitlStore(resolve_state_db_path())
    try:
        yield opened
    finally:
        opened.close()


def _request() -> DecisionRequest:
    """The failure-review question, as the caller asks it."""
    return DecisionRequest(
        plan_id=PLAN_ID,
        source="failure_review",
        question="[failure_review] 问题\n建议：建议",
        options=["ack"],
        default_option="ack",
    )


def _write_config(plan_dir: Path, hooks: List[Dict[str, Any]]) -> None:
    """Give the plan a configuration with ``hooks`` as its outlet."""
    atomic_write_json(
        plan_dir / "hitl_config.json",
        {
            "version": 1,
            "mode": "auto",
            "timeout_sec": 1800,
            "hooks": list(hooks),
        },
        reraise=True,
    )


def _dispatch_audit(plan_dir: Path) -> List[Dict[str, Any]]:
    """The dispatch audit mirror of ``plan_dir``, as a list of records."""
    path = plan_dir / "hooks_audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _NobodyWasReached:
    """A dispatcher whose delivery reaches nobody.

    It stands in for the real dispatcher so the worker thread's decision
    is observable without a subprocess or a socket: the plan names a
    destination, the destination takes the question from nobody.
    """

    def __init__(self) -> None:
        self.attempted = threading.Event()

    def __call__(self, event, payload, plan_dir) -> DispatchReport:
        self.attempted.set()
        return DispatchReport(
            delivered=0, timed_out=0, skipped=1, failed=1,
        )


# ---------------------------------------------------------------------------
# The three rules
# ---------------------------------------------------------------------------


def test_default_still_settles_unroutable(plan_dir: Path, store: HitlStore) -> None:
    """Without the flag, a question nobody was asked is still settled.

    This is the behaviour every existing caller has today, and the
    default has to keep it: the flag is opt-in, so a caller that does not
    know about it sees exactly what it saw before.
    """
    decision = create_decision(_request(), plan_dir, store=store)

    assert decision.status == DecisionStatus.UNROUTABLE.value


def test_flag_false_keeps_pending(plan_dir: Path, store: HitlStore) -> None:
    """With the flag off, the same question stays open for an answer."""
    decision = create_decision(
        _request(), plan_dir, store=store, settle_unrouted=False,
    )

    assert decision.status == DecisionStatus.PENDING.value


def test_pending_decision_can_be_answered(plan_dir: Path, store: HitlStore) -> None:
    """A question kept open can actually be answered.

    This is the whole point of the flag: a question that stays
    ``pending`` is a question the answer API takes.  Had the question
    been settled as ``unroutable``, the answer would have been refused
    with ``409`` and the panel would have had nothing to show.
    """
    decision = create_decision(
        _request(), plan_dir, store=store, settle_unrouted=False,
    )

    status, code = answer_decision(decision.decision_id, "ack", store=store)

    assert (status, code) == (DecisionStatus.ANSWERED.value, 200)


# ---------------------------------------------------------------------------
# The delivery is untouched
# ---------------------------------------------------------------------------


def test_delivery_is_unchanged_when_the_flag_is_off(
    plan_dir: Path, store: HitlStore
) -> None:
    """A plan with no binding still dispatches, and still only logs.

    The flag is about the *settlement*, not about the delivery: the
    question is still handed to the dispatcher, and the dispatch is
    still recorded in the audit mirror.  A plan with no binding is not
    an error — it is a plan whose question nobody was asked.
    """
    create_decision(
        _request(), plan_dir, store=store, settle_unrouted=False,
    )

    records = _dispatch_audit(plan_dir)
    assert [r["event"] for r in records] == [EVENT_DECISION_CREATED], (
        "the question must still be dispatched, and the dispatch recorded; "
        f"got {records!r}"
    )
    assert records[0]["delivered"] == 0, records[0]


# ---------------------------------------------------------------------------
# The second settlement path
# ---------------------------------------------------------------------------


def test_background_settlement_still_happens_by_default(
    plan_dir: Path, store: HitlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan that names a destination still settles when it reaches nobody.

    This is the *other* settlement path — the one on the worker thread —
    and it is what the default has to keep doing.
    """
    _write_config(plan_dir, [{"kind": "cmd", "command": "true"}])
    monkeypatch.setattr("hitl.service.dispatch", _NobodyWasReached())

    decision = create_decision(_request(), plan_dir, store=store)

    deadline = time.monotonic() + GRACE_SEC
    stored = store.get(decision.decision_id)
    while (
        time.monotonic() < deadline
        and stored is not None
        and stored.status != DecisionStatus.UNROUTABLE.value
    ):
        time.sleep(0.02)
        stored = store.get(decision.decision_id)

    assert stored is not None
    assert stored.status == DecisionStatus.UNROUTABLE.value, (
        "a delivery that reached nobody must still settle the question "
        "when the caller asked for the old behaviour"
    )


def test_flag_false_keeps_pending_on_the_background_path(
    plan_dir: Path, store: HitlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag holds on the worker thread as well.

    Without this, the question would be created ``pending`` and closed
    the moment the delivery finished — the flag would only have bought
    the caller the time between the two.
    """
    _write_config(plan_dir, [{"kind": "cmd", "command": "true"}])
    delivery = _NobodyWasReached()
    monkeypatch.setattr("hitl.service.dispatch", delivery)

    decision = create_decision(
        _request(), plan_dir, store=store, settle_unrouted=False,
    )

    assert delivery.attempted.wait(GRACE_SEC), (
        "the delivery was never attempted"
    )
    time.sleep(SETTLE_GRACE_SEC)

    stored = store.get(decision.decision_id)
    assert stored is not None
    assert stored.status == DecisionStatus.PENDING.value, (
        "the question must stay answerable after a delivery that reached "
        "nobody"
    )
