"""The verification tick owns the plan's human-in-the-loop deadlines.

Background
----------
A verification round that asks a human a question has to live with the
answer not coming, and the two kinds of question end differently:

* an **auto** question carries a default answer, so when its deadline
  passes the default becomes the answer and the round carries on;
* a **strict** question has no substitute for a human, so its default
  answer must never be used.  The round stops instead: the decision
  becomes ``paused`` — still answerable — and the plan gets a
  ``pause_state.json`` naming it, so the resume path can find the
  question again instead of guessing which one it was.

``verification_loop._lazy_check_verification`` is the verification
side's periodic entry, and it is the only thing that runs while a
round is blocked on a human, so it is where both deadlines are
noticed.

What is pinned here
-------------------
1. **The tick runs the expiry sweep.**  ``expire_due`` knows *how* an
   overdue question resolves; the tick decides *when*.
2. **A strict timeout pauses, it never answers.**  The decision ends
   ``paused``, its answer stays empty — the default option is exactly
   the answer the backend is not allowed to give — and the plan's
   phase does not advance.
3. **The pause is recorded.**  ``pause_state.json`` carries the kind,
   the reason and the id of the decision that caused it.
4. **A human can still answer.**  A paused decision is answerable, the
   answer is a 200, and the existing resume path still finds the
   interrupted round.
5. **A failing sweep does not end the tick**, and an overdue auto
   question keeps its default and lets the round continue.
6. **The pause releases the wait.**  A round that is blocked in
   ``wait_for_decision`` is let go by the pause — and it is let go with
   the paused question itself, carrying no answer, because nobody gave
   one.
7. **Only the verification side's questions are paused.**  A strict
   question the *execution* side asked stays ``pending``: the
   executor's own gate holds its task and the tasks downstream of it,
   and the verification tick must not take that question over — not by
   pausing it, and not by writing a checkpoint for a round that never
   waited on it.
8. **A question nobody claimed is the round's own.**  The two sides a
   question can be asked from are ``verification`` and ``execution``; a
   question asked through the generic route that nobody claimed is not
   the executor's, so it is paused like the verification side's — with
   the same checkpoint.

Time is injected, never slept
-----------------------------
The tick reads its clock through ``verification_loop._hitl_now``, so
a test freezes the clock instead of waiting for a deadline to pass.
The deadline is derived from the same instant the question was asked
at, so "ten seconds past the deadline" is an exact instant rather
than a race — which is also what keeps this file out of the
``time_sensitive`` lane.

Test command::

    pytest backend/tests/integration/test_verification_loop_expire_due.py -v
"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
import verification_loop  # noqa: E402
from config_paths import resolve_plans_dir, resolve_state_db_path  # noqa: E402
from hitl import service  # noqa: E402
from hitl.models import Decision, DecisionRequest, DecisionStatus  # noqa: E402
from hitl.store import HitlStore  # noqa: E402
from hitl.waiter import wait_for_decision  # noqa: E402
from hooks import config_path, write_for_plan  # noqa: E402
from pause_state import read_pause_checkpoint  # noqa: E402
from routes import verification as verification_routes  # noqa: E402
from routes.hitl import DEFAULT_SOURCE as UNCLAIMED_DECISION_SOURCE  # noqa: E402
from utils.atomic_io import atomic_write_json  # noqa: E402


client = TestClient(server.app)

#: A plan id that is valid under the shared validator and that no other
#: test in the suite uses.
PLAN_ID = "plan-verification-hitl"

#: The instant every question in this file is asked at.  A fixed clock
#: is what makes "ten seconds past the deadline" an exact instant
#: instead of a race against the wall clock.
NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)

#: How long the question waits.  Short enough that the deadline can be
#: put in the past without anybody waiting for it.
TIMEOUT_SEC = 2

#: The answer an overdue *auto* question falls back to.
DEFAULT_OPTION = "accept"

#: A destination that always succeeds, so the question is asked of
#: somebody.  The ``cmd`` transport runs the command and hands it the
#: payload on stdin; ``true`` exits 0 and touches nothing.  The
#: delivery runs on its own thread, so it never holds the decision's
#: creation path.
ASKABLE_HOOKS: List[Dict[str, Any]] = [{"kind": "cmd", "command": "true"}]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_dir() -> Path:
    """The plan's directory — where its ``hitl_config.json`` lives."""
    path = resolve_plans_dir() / PLAN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_config(plan_dir: Path, mode: str, timeout_sec: int) -> None:
    """Write the plan's HITL configuration, with somebody to ask.

    The mode and the deadline are written by the product's own writer,
    so they are the ones the product would store.  The outlet is then
    given a destination the plan's *origin* cannot express:
    ``write_for_plan`` only ever writes the ``cc_connect`` hook a
    session implies, and a question nobody can be asked is
    ``unroutable`` from the start — there is nothing to wait for and
    nothing to pause.
    """
    document = write_for_plan(plan_dir, {"mode": mode, "timeout_sec": timeout_sec})
    document["hooks"] = list(ASKABLE_HOOKS)
    atomic_write_json(config_path(plan_dir), document, reraise=True)


def _ask(
    *,
    mode: str,
    timeout_sec: int = TIMEOUT_SEC,
    default_option: Optional[str] = DEFAULT_OPTION,
    source: str = "verification",
    task_id: Optional[str] = None,
    now: datetime = NOW,
) -> Decision:
    """Ask the plan's question, at ``NOW``, in the plan's own mode.

    The plan's ``hitl_config.json`` is written first, because the
    plan's configuration — not the caller's request — is what decides
    the mode and the deadline the question is stored with.

    The **source** is who asked: the verification side by default, the
    execution side when the question is about a task.  The two end
    differently when nobody answers, so the tests that care about the
    difference ask on each side.
    """
    plan_dir = _plan_dir()
    _write_config(plan_dir, mode, timeout_sec)
    request = DecisionRequest(
        plan_id=PLAN_ID,
        source=source,
        task_id=task_id,
        question="Ship the change as written?",
        options=["accept", "reject"],
        default_option=default_option,
        mode=mode,
        timeout_sec=timeout_sec,
    )
    with HitlStore(resolve_state_db_path()) as store:
        return service.create_decision(request, plan_dir, store=store, now=now)


def _stored(decision_id: str) -> Decision:
    """The stored form of ``decision_id``, read fresh."""
    with HitlStore(resolve_state_db_path()) as store:
        decision = store.get(decision_id)
    assert decision is not None, f"decision {decision_id!r} is not stored"
    return decision


def _running_verification(round_n: int = 1) -> None:
    """Put a running verification round into the in-memory state.

    The state is what the watchdog reads to decide whether a plan is
    still verifying; a plan with no entry is a plan with nothing to
    watch.
    """
    stamp = datetime.now(timezone.utc).isoformat()
    server._verification_state[PLAN_ID] = {
        "plan_id": PLAN_ID,
        "verification_status": "running",
        "verification_round": round_n,
        "verification_max_rounds": 3,
        "results": {},
        "repair_tasks": [],
        "started_at": stamp,
        "updated_at": stamp,
        "orchestrator": None,
        "stop_reason": None,
        "thread": None,
    }


def _freeze_the_clock(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """Make the tick read ``moment`` instead of the wall clock."""
    monkeypatch.setattr(verification_loop, "_hitl_now", lambda: moment)


def _past_the_deadline() -> datetime:
    """An instant comfortably past the question's deadline."""
    return NOW + timedelta(seconds=TIMEOUT_SEC + 10)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_verification_state():
    """Leave no running round behind for the next test to trip over."""
    server._verification_state.pop(PLAN_ID, None)
    yield
    server._verification_state.pop(PLAN_ID, None)


# ---------------------------------------------------------------------------
# 1 — the tick runs the expiry sweep
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_tick_calls_expire_due(monkeypatch: pytest.MonkeyPatch) -> None:
    """One tick runs the expiry sweep, against the clock the tick reads.

    The sweep is what resolves an overdue ``auto`` question; the tick
    is what decides *when* it happens.  A tick that did not sweep would
    leave a round waiting for a human long after the question it asked
    already had an answer.
    """
    calls: List[Optional[datetime]] = []

    def spy(now=None, **kwargs):
        calls.append(now)
        return 0

    monkeypatch.setattr(verification_loop, "expire_due", spy)
    _freeze_the_clock(monkeypatch, NOW)

    verification_loop._lazy_check_verification(PLAN_ID)

    assert calls == [NOW], (
        "the tick must run the expiry sweep, and it must run it against "
        "the clock the tick reads — not the wall clock"
    )


# ---------------------------------------------------------------------------
# 2 — a strict timeout pauses, it never answers
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_strict_timeout_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A strict question nobody answered ends ``paused``, not answered.

    Three things have to hold at once, and each of them fails
    differently:

    * the **decision** ends ``paused`` — the question is still open,
      and the backend did not answer it;
    * the **answer** stays empty — the default option is exactly the
      answer a strict question may never be given;
    * the **plan** does not advance — the round is waiting for a human,
      not dead, so the watchdog must not read the pause as a failure.
    """
    created = _ask(mode="strict")
    _running_verification()
    _freeze_the_clock(monkeypatch, _past_the_deadline())

    terminal_calls: List[Any] = []
    monkeypatch.setattr(
        server,
        "_persist_verification_terminal",
        lambda *args, **kwargs: terminal_calls.append(args),
    )

    verification_loop._lazy_check_verification(PLAN_ID)

    stored = _stored(created.decision_id)
    assert stored.status == DecisionStatus.PAUSED.value, (
        "a strict question nobody answered must be paused — the backend "
        "may not answer it in a human's place"
    )
    assert stored.answer is None, (
        "a strict decision must never fall back to its default option; "
        "the default is the one answer the backend is not allowed to give"
    )

    assert terminal_calls == [], (
        "the plan's phase must not advance: the round is waiting for a "
        "human, not dead"
    )
    assert server._verification_state[PLAN_ID]["verification_status"] == "running"


@pytest.mark.integration
def test_strict_timeout_writes_the_pause_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pause is recorded, and it names the question that caused it.

    The checkpoint is the only durable trace of *why* the round stopped,
    and the ``decision_id`` is what lets the resume path find the
    question again instead of guessing which one it was.
    """
    created = _ask(mode="strict")
    _running_verification()
    _freeze_the_clock(monkeypatch, _past_the_deadline())

    verification_loop._lazy_check_verification(PLAN_ID)

    checkpoint = read_pause_checkpoint(_plan_dir())
    assert checkpoint is not None, (
        "a strict timeout must leave a checkpoint — without one the "
        "resume path has nothing to pick the round up from"
    )
    assert checkpoint["kind"] == "verification"
    assert checkpoint["reason"] == "hitl_strict_timeout"
    assert checkpoint["decision_id"] == created.decision_id
    assert checkpoint["verification_round"] == 1


# ---------------------------------------------------------------------------
# 3 — a human can still answer, and the round can still be resumed
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_strict_answer_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A paused question is still answerable, and the answer is a 200.

    The pause is not the end of the question: it is the end of the
    *waiting*.  A human who answers afterwards must be taken seriously
    (the answer is stored, not refused), and the round the pause
    interrupted must still be there for the resume path to find.
    """
    created = _ask(mode="strict")
    _running_verification()
    _freeze_the_clock(monkeypatch, _past_the_deadline())

    verification_loop._lazy_check_verification(PLAN_ID)
    assert _stored(created.decision_id).status == DecisionStatus.PAUSED.value

    response = client.post(
        f"/api/hitl/{PLAN_ID}/decisions/{created.decision_id}/answer",
        json={"option": "accept", "answered_by": "alice", "note": ""},
    )
    assert response.status_code == 200, (
        "a paused decision is still open — the answer must be taken, "
        f"not refused: {response.status_code} {response.text}"
    )

    stored = _stored(created.decision_id)
    assert stored.status == DecisionStatus.ANSWERED.value
    assert stored.answer == "accept"

    # The existing resume path: the start endpoint reads the
    # verification checkpoint and resumes the round it names.
    checkpoint = verification_routes._read_verification_checkpoint(_plan_dir())
    assert checkpoint is not None, (
        "the resume path must still find the interrupted round after the "
        "question was answered"
    )
    assert checkpoint["decision_id"] == created.decision_id

    round_n, resumed = verification_routes._resolve_start_round(
        {"round": 1, "verification_status": "running"}, checkpoint
    )
    assert resumed is True, "the round must be resumed, not replaced"
    assert round_n == 1, "the interrupted round is the one to pick up"


# ---------------------------------------------------------------------------
# 4 — the boundaries
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_auto_question_defaults_and_the_round_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An overdue auto question ends ``defaulted`` and the tick carries on.

    The auto question is the other half of the same deadline, and the
    two must not be confused: it *has* a default answer, so the round
    does not stop, and nothing is written to the pause checkpoint.
    """
    created = _ask(mode="auto")
    _running_verification()
    _freeze_the_clock(monkeypatch, _past_the_deadline())

    terminal_calls: List[Any] = []
    monkeypatch.setattr(
        server,
        "_persist_verification_terminal",
        lambda *args, **kwargs: terminal_calls.append(args),
    )

    verification_loop._lazy_check_verification(PLAN_ID)

    stored = _stored(created.decision_id)
    assert stored.status == DecisionStatus.DEFAULTED.value, (
        "an auto question nobody answered ends by itself, with the "
        "default option it was created with"
    )
    assert stored.answer == DEFAULT_OPTION

    assert read_pause_checkpoint(_plan_dir()) is None, (
        "an auto question that resolved itself does not pause the round"
    )
    assert terminal_calls == [], (
        "the round carries on after an auto question defaults — nothing "
        "about it is a failure"
    )
    assert server._verification_state[PLAN_ID]["verification_status"] == "running"


@pytest.mark.integration
def test_a_failing_sweep_does_not_end_the_tick(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The sweep may fail; the tick after it still runs.

    The sweep writes to a database another thread may be writing to, so
    it can fail.  What it must not do is take the watchdog with it: a
    tick that died on its first expiry would leave every later question
    waiting forever, and the failure that killed it would have been
    swallowed by the tick's caller.
    """
    calls: List[Optional[datetime]] = []

    def flaky_expire_due(now=None, **kwargs):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("the state database is locked")
        return 0

    monkeypatch.setattr(verification_loop, "expire_due", flaky_expire_due)
    _freeze_the_clock(monkeypatch, NOW)

    with caplog.at_level(logging.WARNING, logger="server"):
        verification_loop._lazy_check_verification(PLAN_ID)

    assert len(calls) == 1, "the tick did not run the sweep at all"
    assert any(
        "locked" in record.getMessage() for record in caplog.records
    ), (
        "the failure must be logged — a silent failure is "
        "indistinguishable from a sweep that found nothing to do"
    )

    # The watchdog is still alive: the next tick runs the sweep again.
    verification_loop._lazy_check_verification(PLAN_ID)

    assert len(calls) == 2, (
        "the tick after the failure must still run the sweep — the failed "
        "sweep did not take the watchdog down"
    )


@pytest.mark.integration
def test_a_strict_question_before_its_deadline_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A question nobody answered *yet* is still waiting — not paused.

    The pause is for a question whose deadline has passed.  A question
    that is still inside its window is a question a human may still
    answer, and pausing it early would end the waiting for a deadline
    that has not arrived.
    """
    created = _ask(mode="strict")
    _running_verification()
    _freeze_the_clock(monkeypatch, NOW)

    verification_loop._lazy_check_verification(PLAN_ID)

    stored = _stored(created.decision_id)
    assert stored.status == DecisionStatus.PENDING.value
    assert read_pause_checkpoint(_plan_dir()) is None


@pytest.mark.integration
def test_the_tick_survives_a_plan_without_a_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan that asked nothing is not an error.

    The tick runs for every plan the watchdog watches, and most of them
    have no open question at all.  That is the ordinary case, and it
    must leave no trace: no decision, no checkpoint, no exception.
    """
    _running_verification()
    _freeze_the_clock(monkeypatch, NOW)

    verification_loop._lazy_check_verification(PLAN_ID)

    assert read_pause_checkpoint(_plan_dir()) is None
    assert server._verification_state[PLAN_ID]["verification_status"] == "running"


# ---------------------------------------------------------------------------
# The shape of the checkpoint the pause writes
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_pause_checkpoint_has_the_documented_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checkpoint is the document ``pause_state`` documents.

    The key set is fixed: a checkpoint that grew a key would break the
    readers that know the shape, and one that lost a key would break
    the resume path that reads it.
    """
    created = _ask(mode="strict")
    _running_verification()
    _freeze_the_clock(monkeypatch, _past_the_deadline())

    verification_loop._lazy_check_verification(PLAN_ID)

    checkpoint = read_pause_checkpoint(_plan_dir())
    assert checkpoint is not None
    assert set(checkpoint) == {
        "version",
        "kind",
        "project_dir",
        "paused_at",
        "reason",
        "interrupted_tasks",
        "unfinished_tasks",
        "verification_round",
        "decision_id",
    }
    assert checkpoint["decision_id"] == created.decision_id
    assert checkpoint["interrupted_tasks"] == [], (
        "no task was caught mid-flight: the pause happened at the "
        "question, not inside a task"
    )


# ---------------------------------------------------------------------------
# 5 — the pause releases the wait it interrupted
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_pause_releases_a_blocked_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round blocked in ``wait_for_decision`` is let go by the pause.

    The scenario this file describes is a round that asks a question
    and then waits for it.  A pause that did not release that wait
    would leave the round blocked forever: the plan paused, the
    thread still waiting, and nothing left to notice an answer that
    arrives later.

    So the pause is not only a status change — it is what ends the
    wait.  What it ends it with is the paused question itself, with no
    answer attached, because nobody gave one: the default option is
    the answer the backend is not allowed to give.
    """
    created = _ask(mode="strict")
    _running_verification()
    _freeze_the_clock(monkeypatch, _past_the_deadline())

    returned: List[Decision] = []

    def _wait() -> None:
        with HitlStore(resolve_state_db_path()) as waiter_store:
            returned.append(
                wait_for_decision(
                    created.decision_id,
                    store=waiter_store,
                    poll_interval=0.01,
                    heartbeat_interval=60.0,
                )
            )

    thread = threading.Thread(
        target=_wait, name="test-hitl-pause-waiter", daemon=True
    )
    thread.start()
    try:
        thread.join(timeout=0.5)
        assert thread.is_alive(), (
            "the waiter was never blocked — the round would have been "
            "waiting for nothing, and the rest of the test would prove "
            "nothing at all"
        )

        verification_loop._lazy_check_verification(PLAN_ID)

        thread.join(timeout=5.0)
    finally:
        thread.join(timeout=5.0)

    assert not thread.is_alive(), (
        "the pause did not release the blocked waiter — the round is "
        "stuck on a question nobody is going to answer"
    )
    assert len(returned) == 1
    assert returned[0].decision_id == created.decision_id
    assert returned[0].status == DecisionStatus.PAUSED.value
    assert returned[0].answer is None, (
        "the wait ended with the paused question, not with an answer — "
        "nobody gave one"
    )


# ---------------------------------------------------------------------------
# 6 — the strict timeout belongs to the verification side
# ---------------------------------------------------------------------------
#
# Two sides ask questions, and they end differently when nobody
# answers.
#
# The **verification** side asks about the plan.  Its strict question
# running out of time stops the *round*: the question is paused and
# the plan gets a ``pause_state.json`` naming it.
#
# The **execution** side asks about a *task*.  Its strict question
# running out of time stops *that task and the tasks downstream of
# it* — which the executor's own gate already does.  The question
# itself stays ``pending``: a human may still answer it, and the plan
# keeps listing it as outstanding.
#
# The sweep that pauses the verification side's questions has to tell
# the two apart.  A sweep that does not would pause the execution
# side's question as well — and that question would then vanish from
# the ``pending`` list the operator's to-do reads, while the
# checkpoint written beside it named a question the verification round
# never asked about.
#
# The same rule settles the question nobody claimed: the architecture
# has two sides, and a question the executor did not ask is the
# round's — so it is paused, and the checkpoint names it.


def _write_tasks() -> None:
    """Write the plan's task graph.

    ``B`` is the task the execution side's question is about, and
    ``C`` depends on it — so the gate has something downstream to hold
    as well.
    """
    atomic_write_json(
        _plan_dir() / "tasks.json",
        {
            "requirement": "hitl verification deadlines",
            "tasks": [
                {"id": "A", "depends_on": []},
                {"id": "B", "depends_on": ["A"]},
                {"id": "C", "depends_on": ["B"]},
            ],
        },
        reraise=True,
    )


@pytest.mark.integration
def test_execution_side_strict_timeout_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strict question the execution side asked is not the round's.

    A missed deadline on the execution side means the task the
    question is about — and everything downstream of it — stays out of
    the schedule.  That is the executor's own gate
    (``hitl.service.blocked_task_ids``) doing it, and it keeps doing
    it while the question is ``pending``.

    The verification tick must not take the question over.  It may not
    pause it — a paused question leaves the ``pending`` list the
    operator reads, so the plan would sit on a problem nobody can see
    any more — and it may not write a pause checkpoint for a round
    that never waited on it.
    """
    _write_tasks()
    created = _ask(mode="strict", source="execution", task_id="B")
    _running_verification()
    _freeze_the_clock(monkeypatch, _past_the_deadline())

    verification_loop._lazy_check_verification(PLAN_ID)

    stored = _stored(created.decision_id)
    assert stored.status == DecisionStatus.PENDING.value, (
        "a question the execution side asked belongs to the executor: "
        f"the verification tick paused it ({stored.status!r}), and a "
        "paused question is gone from the pending list the operator reads"
    )
    assert stored.answer is None, (
        "nobody answered the question, so it has no answer"
    )
    assert read_pause_checkpoint(_plan_dir()) is None, (
        "the verification tick wrote a pause checkpoint for a round that "
        "never waited on anything: the checkpoint is the verification "
        "side's, and this question is not its own"
    )

    blocked = service.blocked_task_ids(PLAN_ID)
    assert "B" in blocked, (
        "the execution side's gate must still hold the task the question "
        f"is about; blocked={sorted(blocked)!r}"
    )
    assert "C" in blocked, (
        "the execution side's gate must still hold the tasks downstream of "
        f"the question's task; blocked={sorted(blocked)!r}"
    )


@pytest.mark.integration
def test_execution_side_does_not_steal_the_verification_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two overdue strict questions, one on each side: one checkpoint.

    The checkpoint says why the *verification* round stopped and which
    question stopped it.  The execution side's question is not what
    stopped the round, so it is not what the checkpoint names — and it
    stays ``pending``, because the executor's gate is what holds its
    task and the tasks downstream of it.

    The execution side's question is asked first so that it is the
    *older* of the two: a sweep that paused both would name the older
    one, and the checkpoint would point at a question the verification
    round never asked about.
    """
    _write_tasks()
    execution_side = _ask(mode="strict", source="execution", task_id="B")
    verification_side = _ask(mode="strict", now=NOW + timedelta(seconds=1))
    _running_verification()
    _freeze_the_clock(monkeypatch, _past_the_deadline())

    verification_loop._lazy_check_verification(PLAN_ID)

    checkpoint = read_pause_checkpoint(_plan_dir())
    assert checkpoint is not None, (
        "the verification side's question ran out of time, so the round "
        "stopped and the pause must be recorded"
    )
    assert checkpoint["kind"] == "verification"
    assert checkpoint["reason"] == "hitl_strict_timeout"
    assert checkpoint["decision_id"] == verification_side.decision_id, (
        "the checkpoint must name the question that stopped the round — "
        "the verification side's — and not the execution side's"
    )

    assert (
        _stored(execution_side.decision_id).status == DecisionStatus.PENDING.value
    ), (
        "the execution side's question is not the round's: it stays "
        "pending, and the executor's gate keeps holding its task"
    )


@pytest.mark.integration
def test_a_question_nobody_claimed_is_the_round_s_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A question nobody claimed is the round's own.

    A question asked through the generic route carries the source the
    route records for a decision *nobody else claimed*.  It is not the
    executor's question: the executor did not ask it, and the
    executor's gate has nothing to hold up because of it.

    The architecture has two sides a question can be asked from, and the
    executor is one of them — so a question nobody claimed is the round's,
    and the round's own strict-expiry rule is what happens to it: the
    question is paused and the plan gets a ``pause_state.json`` naming
    it.  This is the half of the rule the execution-side case does not
    cover; without it, a question asked of a human through the route
    would never be noticed as overdue, and the plan would keep waiting
    for an answer that only a person asked directly can give.
    """
    created = _ask(mode="strict", source=UNCLAIMED_DECISION_SOURCE)
    _running_verification()
    _freeze_the_clock(monkeypatch, _past_the_deadline())

    verification_loop._lazy_check_verification(PLAN_ID)

    stored = _stored(created.decision_id)
    assert stored.status == DecisionStatus.PAUSED.value, (
        "a question nobody claimed is not the executor's, so the round's "
        f"own strict-expiry rule applies; the sweep left it {stored.status!r}"
    )
    assert stored.answer is None, (
        "nobody answered the question, so it has no answer"
    )

    checkpoint = read_pause_checkpoint(_plan_dir())
    assert checkpoint is not None, (
        "the question the round was waiting for ran out of time, so the "
        "round stopped and the pause must be recorded"
    )
    assert checkpoint["kind"] == "verification"
    assert checkpoint["reason"] == "hitl_strict_timeout"
    assert checkpoint["decision_id"] == created.decision_id, (
        "the checkpoint must name the question that stopped the round"
    )
