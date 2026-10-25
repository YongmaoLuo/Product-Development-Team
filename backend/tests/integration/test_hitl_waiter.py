"""Waiting for a human: the poll loop, the heartbeat, and the way out.

Background
----------
A thread that waits for a human waits for a *long* time: the question
may sit unanswered for minutes, sometimes for hours.  Two things have
to be true while it waits.

**The wait must not look like a hang.**  The backend's watchdog judges a
thread that has produced nothing for 900 seconds to be dead
(``VERIFICATION_WATCHDOG_STALENESS_SECONDS``).  A thread blocked on a
human produces nothing *by definition* — it is waiting, not working —
so without a heartbeat the watchdog kills a perfectly healthy wait.
The waiter therefore says something every 60 seconds: long before the
900-second window closes, and often enough that a single missed beat
is not a missed window.

**The wait must end when the question is settled.**  The question is
answered by somebody else, in another process, minutes later, so the
waiter cannot be *told* — it has to look.  Every ``poll_interval``
seconds it re-reads the row and reads the status.

What is asserted here
---------------------
The three properties the waiter exists for:

  * ``test_heartbeat_every_60s`` — one heartbeat per minute of waiting,
    and not one more (a heartbeat that fires twice in a minute is as
    much a lie as one that never fires).
  * ``test_returns_on_answer`` — the wait ends as soon as a human
    answers, and it ends with the answer.
  * ``test_no_residual_thread`` — a finished wait leaves no thread
    behind, because a thread that outlives its wait is a thread that
    outlives its store.

and the three boundaries around them: the waiter does not resolve a
question before its deadline (the *service* does that, not the
waiter), a heartbeat callback that raises does not take the wait down
with it, and a released wait ends promptly instead of hanging.

Time is injected, never slept
------------------------------
``now_fn`` is the waiter's clock, so "ten minutes of waiting" is
asserted by moving a clock, not by waiting ten minutes.  The only real
time the suite spends is the few milliseconds a poll loop needs to
notice a change.  That is also what keeps this file out of the
``time_sensitive`` lane.

Test command::

    pytest backend/tests/integration/test_hitl_waiter.py -v
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from config_paths import resolve_plans_dir, resolve_state_db_path
from hitl import waiter as waiter_module
from hitl.models import Decision, DecisionMode, DecisionRequest, DecisionStatus
from hitl.store import HitlStore
from hitl.waiter import DecisionWaiter, WaiterReleased, wait_for_decision

#: The instant every decision in this file is created at.  A fixed clock
#: is what makes "one second past the deadline" an exact instant
#: instead of a race against the wall clock.
NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)

#: The prefix of the waiter's thread name.  The residual-thread test
#: looks for it by name, because a waiter that outlives its wait would
#: otherwise be invisible in a thread list full of pytest's own.
THREAD_PREFIX = "hitl-wait-"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock the test moves by hand.

    The waiter reads it through ``now_fn``, so the whole minute-long
    heartbeat schedule is asserted without the suite spending a
    minute.
    """

    def __init__(self, start: datetime = NOW) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        self._now = self._now + timedelta(seconds=seconds)


def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 5.0
) -> bool:
    """Wait for ``predicate`` to become true, up to ``timeout`` seconds.

    The waiter runs in its own thread, so "the answer has been seen"
    is a fact about another thread: the test waits for it rather than
    assuming it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _request(plan_id: str, **overrides: Any) -> DecisionRequest:
    """A valid request; ``overrides`` replaces single fields."""
    payload: Dict[str, Any] = {
        "plan_id": plan_id,
        "source": "execution",
        "task_id": "T-1",
        "question": "Retry the failing task?",
        "options": ["accept", "reject"],
        "default_option": "accept",
        "mode": DecisionMode.AUTO.value,
        "timeout_sec": None,
    }
    payload.update(overrides)
    return DecisionRequest(**payload)


def _create(
    store: HitlStore,
    *,
    plan_id: str = "plan-hitl-waiter",
    timeout_sec: Optional[int] = None,
    now: datetime = NOW,
    source: str = "execution",
    vp_id: Optional[str] = None,
    round_number: Optional[int] = None,
) -> Decision:
    """Store a pending decision and return it.

    Built through the model and the store rather than through
    :func:`hitl.service.create_decision`, because this file needs a
    decision with *no* deadline (``timeout_sec=None``) — a question
    that waits for a human for as long as it takes — and the service
    takes the timeout from the plan's configuration.
    """
    decision = Decision.from_request(
        _request(
            plan_id,
            timeout_sec=timeout_sec,
            source=source,
            vp_id=vp_id,
            round=round_number,
        ),
        now=now,
    )
    return store.insert(decision)


def _audit(plan_id: str = "plan-hitl-waiter") -> List[Dict[str, Any]]:
    """The audit mirror of ``plan_id``, as a list of records."""
    path = resolve_plans_dir() / plan_id / "hitl_decisions.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _heartbeat_of(store: HitlStore, decision_id: str) -> Optional[str]:
    """The heartbeat stamp on ``decision_id``, read fresh.

    ``None`` when the decision is gone or carries no stamp — the two
    are the same answer here, because both mean "nobody has beaten for
    this question".
    """
    decision = store.get(decision_id)
    if decision is None:
        return None
    return decision.heartbeat_at


class _SlowStore:
    """A store whose reads take a moment.

    The waiter only ever calls ``get()`` on it, so that is all this
    proxy has.  The delay is the point: a worker that is still inside a
    read when the teardown looks is a worker the teardown did not
    join, and a worker that reads instantly would have exited by
    itself before anybody could tell the difference.
    """

    def __init__(self, store: HitlStore, delay: float = 0.3) -> None:
        self._store = store
        self._delay = delay

    def get(self, decision_id: str) -> Optional[Decision]:
        time.sleep(self._delay)
        return self._store.get(decision_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> HitlStore:
    """A store on the per-test database, with the plans root as audit root."""
    opened = HitlStore(resolve_state_db_path())
    try:
        yield opened
    finally:
        opened.close()


# ---------------------------------------------------------------------------
# 1. The heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_heartbeat_every_60s(store: HitlStore) -> None:
    """One heartbeat per minute of waiting — and not one more.

    The clock is injected, so the assertion is about *decision time*
    rather than about wall-clock seconds: the clock is moved to the
    half-minute, then a minute at a time, and the waiter must answer
    with exactly one heartbeat per minute — 20 of them over 1200 s.

    The count is the point.  A heartbeat that fires twice in a minute
    is as much a lie as one that never fires: the first says the thread
    is busier than it is, the second says the watchdog may kill a
    thread that is perfectly alive.  The waiter therefore counts the
    *intervals* that have passed, not the number of times its poll
    loop happened to run — a poll that was late by a minute still owes
    exactly one heartbeat for that minute.
    """
    clock = FakeClock()
    decision = _create(store, timeout_sec=None)
    beats: List[datetime] = []

    waiter = DecisionWaiter(
        decision.decision_id,
        store=store,
        on_heartbeat=lambda: beats.append(clock()),
        poll_interval=0.01,
        heartbeat_interval=60.0,
        now_fn=clock,
    )
    waiter.start()
    try:
        # Nothing before the first minute.  A heartbeat that fires
        # early is a heartbeat that lies: it says the thread has waited
        # longer than it has, and it shortens the interval every
        # heartbeat is supposed to buy.
        clock.advance(30.0)
        time.sleep(0.1)
        assert beats == [], "a heartbeat arrived before the first interval"

        # One beat per minute from then on: 19 more minutes take the
        # clock to 1200 s and the count to 20.
        clock.advance(30.0)
        assert _wait_until(lambda: len(beats) >= 1), "the first heartbeat never arrived"
        for expected in range(2, 21):
            clock.advance(60.0)
            assert _wait_until(lambda: len(beats) >= expected), (
                f"heartbeat #{expected} never arrived"
            )
        # Let the loop poll a few more times: a heartbeat *here* would
        # mean the interval is not what it claims to be.
        time.sleep(0.1)
        assert len(beats) == 20
        gaps = [
            (later - earlier).total_seconds()
            for earlier, later in zip(beats, beats[1:])
        ]
        assert gaps == [60.0] * 19
    finally:
        waiter.release()
        waiter.join(timeout=5.0)


@pytest.mark.integration
def test_heartbeat_catches_up_after_a_clock_jump(store: HitlStore) -> None:
    """A clock that jumps 1200 s owes twenty heartbeats — not one, not none.

    The schedule is anchored at the *start of the wait*, not at the
    moment the worker thread happened to be scheduled.  That matters
    because the two are not the same instant: a clock that jumps
    forward between the start of the wait and the worker's first read
    must still be counted from the start, or the number of heartbeats
    would be a fact about the scheduler rather than a fact about the
    clock.

    The count is 20 either way the clock is moved — one 1200-second
    jump, or twenty 60-second steps — because the count is the number
    of *intervals* that have passed, not the number of times the poll
    loop ran.
    """
    clock = FakeClock()
    decision = _create(store, timeout_sec=None)
    beats: List[datetime] = []

    waiter = DecisionWaiter(
        decision.decision_id,
        store=store,
        on_heartbeat=lambda: beats.append(clock()),
        poll_interval=0.01,
        heartbeat_interval=60.0,
        now_fn=clock,
    )
    waiter.start()
    try:
        clock.advance(1200.0)  # twenty minutes, in one jump
        assert _wait_until(lambda: len(beats) >= 20), (
            f"only {len(beats)} heartbeats after a 1200 s jump"
        )
        time.sleep(0.1)
        assert len(beats) == 20
    finally:
        waiter.release()
        waiter.join(timeout=5.0)


@pytest.mark.integration
def test_heartbeat_schedule_is_anchored_at_the_start_of_the_wait(
    store: HitlStore,
) -> None:
    """The schedule starts when the wait starts, not when the worker runs.

    The origin reading — the instant the heartbeat schedule counts
    from — is taken by the *caller*, in ``start()``.  A schedule
    anchored at whatever moment the worker thread happened to be
    scheduled would make the number of heartbeats a fact about the
    scheduler rather than a fact about the clock: two runs of the same
    wait could disagree about how many minutes had passed.

    So the first reading of the clock is the caller's, and it happens
    before ``start()`` returns.
    """
    fake = FakeClock()
    readers: List[str] = []

    def clock() -> datetime:
        readers.append(threading.current_thread().name)
        return fake()

    decision = _create(store, timeout_sec=None)
    waiter = DecisionWaiter(
        decision.decision_id,
        store=store,
        poll_interval=0.01,
        heartbeat_interval=60.0,
        now_fn=clock,
    )
    waiter.start()
    try:
        assert readers, "the clock was never read"
        assert readers[0] == threading.current_thread().name, (
            "the schedule was anchored at the worker's first reading "
            f"({readers[0]!r}), not at the start of the wait"
        )
    finally:
        waiter.release()
        waiter.join(timeout=5.0)


@pytest.mark.integration
def test_heartbeat_failure_does_not_stop_the_wait(
    store: HitlStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A heartbeat callback that raises is logged, not fatal.

    The callback is the caller's own code — the thing that stamps
    ``heartbeat_at`` — and it may fail for reasons that have nothing
    to do with the question (a full disk, a closed connection).  The
    wait is still a wait: the thread is still alive, so the right
    answer is to log the failure and keep waiting, not to unwind a
    wait that a human is about to answer.
    """
    clock = FakeClock()
    decision = _create(store, timeout_sec=None)
    calls: List[datetime] = []

    def mark() -> None:
        calls.append(clock())
        raise RuntimeError("the heartbeat sink is down")

    waiter = DecisionWaiter(
        decision.decision_id,
        store=store,
        on_heartbeat=mark,
        poll_interval=0.01,
        heartbeat_interval=60.0,
        now_fn=clock,
    )
    waiter.start()
    try:
        with caplog.at_level(logging.WARNING, logger="hitl.waiter"):
            clock.advance(60.0)
            assert _wait_until(lambda: len(calls) >= 1), "the first heartbeat never ran"
            clock.advance(60.0)
            assert _wait_until(lambda: len(calls) >= 2), (
                "the wait stopped after the heartbeat callback raised"
            )
        assert waiter.finished is False, "the wait ended although nobody answered"
        assert decision.decision_id in caplog.text
    finally:
        waiter.release()
        waiter.join(timeout=5.0)


# ---------------------------------------------------------------------------
# 2. The end of the wait
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_returns_on_answer(store: HitlStore) -> None:
    """The wait ends as soon as a human answers — and it ends with it.

    The answer comes from the store, not from a callback: the waiter
    is looking at the same row every other actor is looking at, so
    what it returns is what everybody else sees.
    """
    decision = _create(store, timeout_sec=None)
    answers: List[Decision] = []

    def _wait() -> None:
        answers.append(
            wait_for_decision(
                decision.decision_id,
                store=store,
                poll_interval=0.01,
                heartbeat_interval=60.0,
            )
        )

    thread = threading.Thread(target=_wait, name="test-hitl-waiter", daemon=True)
    thread.start()
    try:
        assert store.answer_if_pending(
            decision.decision_id, "accept", answered_by="tester"
        ), "the answer was not stored"
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "the wait did not end after the answer"
    finally:
        thread.join(timeout=5.0)

    assert len(answers) == 1
    assert answers[0].decision_id == decision.decision_id
    assert answers[0].status == DecisionStatus.ANSWERED.value
    assert answers[0].answer == "accept"


@pytest.mark.integration
def test_no_residual_thread(store: HitlStore) -> None:
    """A finished wait leaves no thread behind.

    A waiter that outlives its wait outlives its store: the thread
    would keep polling a connection that may already be closed, and
    the process would not be able to shut down.

    Both forms of the waiter are checked, because they carry the
    promise in different ways.

    :func:`wait_for_decision` owns the worker's whole life, so the
    check is made *inside* the waiting thread, in the instant the call
    returns: that is the moment the promise is about.  A worker still
    winding down would be visible here and nowhere else — a moment
    later it would have exited by itself and the leak would be
    invisible.

    :class:`DecisionWaiter` hands the thread to its caller, so the
    teardown owns it: release, then join.  After the join the worker
    is provably dead, not merely about to be.
    """
    decision = _create(store, timeout_sec=None)
    before = {thread.name for thread in threading.enumerate()}

    # -- the one-call form ------------------------------------------------
    answers: List[Decision] = []
    leftover: List[str] = []

    def _wait() -> None:
        answers.append(
            wait_for_decision(
                decision.decision_id,
                store=store,
                poll_interval=0.01,
                heartbeat_interval=60.0,
            )
        )
        leftover.extend(
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith(THREAD_PREFIX)
        )

    thread = threading.Thread(target=_wait, name="test-hitl-waiter", daemon=True)
    thread.start()
    try:
        assert store.answer_if_pending(
            decision.decision_id, "accept", answered_by="tester"
        )
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "the wait did not end after the answer"
    finally:
        thread.join(timeout=5.0)

    assert leftover == [], f"a waiter thread outlived the wait: {leftover}"
    assert {t.name for t in threading.enumerate()} <= before, "leaked threads"

    # -- the caller-owned form --------------------------------------------
    # The reads are deliberately slow here, so that a worker the
    # teardown failed to join is still inside a read when the teardown
    # looks — rather than having exited by itself a microsecond
    # earlier and made the leak invisible.  The wait is *not* answered
    # here: the point is the teardown, and a teardown that joins is
    # the only thing that can end a worker that is still polling.
    slow = _SlowStore(store, delay=0.3)
    other = _create(store, timeout_sec=None)
    waiter = DecisionWaiter(
        other.decision_id,
        store=slow,  # type: ignore[arg-type] - the waiter only calls get()
        poll_interval=0.01,
        heartbeat_interval=60.0,
    )
    waiter.start()
    try:
        time.sleep(0.05)  # let the worker get into a read
    finally:
        # The teardown owns the thread: release it, then join it.
        waiter.release()
        waiter.join(timeout=5.0)

    assert not waiter.thread.is_alive(), "the worker outlived the wait"
    assert {t.name for t in threading.enumerate()} <= before, "leaked threads"


# ---------------------------------------------------------------------------
# 3. The boundaries
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_does_not_return_before_the_deadline(store: HitlStore) -> None:
    """Before the deadline the waiter keeps waiting.

    The question is not the waiter's to answer: until the deadline
    arrives there is nothing to resolve, and a waiter that gave up
    early would report a decision nobody made.  The row is still
    ``pending`` and the wait is still running.
    """
    clock = FakeClock()
    decision = _create(store, timeout_sec=60, now=NOW)

    waiter = DecisionWaiter(
        decision.decision_id,
        store=store,
        poll_interval=0.01,
        heartbeat_interval=60.0,
        now_fn=clock,
    )
    waiter.start()
    try:
        clock.advance(30.0)  # half the waiting window
        time.sleep(0.1)
        assert waiter.finished is False, "the wait ended before the deadline"
        stored = store.get(decision.decision_id)
        assert stored is not None
        assert stored.status == DecisionStatus.PENDING.value
    finally:
        waiter.release()
        waiter.join(timeout=5.0)


@pytest.mark.integration
def test_returns_defaulted_once_the_service_expires_it(
    store: HitlStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the deadline the *service* migrates the row, and the wait ends.

    Two things are asserted, and they are different things.

    The wait ends with the ``defaulted`` decision — the caller gets
    the same row everybody else sees, with the ``default_option`` as
    its answer.

    And the migration went through :func:`hitl.service.expire_due`,
    with the injected clock's reading: the waiter asks the service to
    resolve the question and then reads the result, rather than
    writing the row behind the service's back.  A waiter that
    resolved the question itself would leave an audit trail nobody
    authorised — and the sweep it skipped is the one that settles
    *every* due decision, not just this one.
    """
    clock = FakeClock()
    decision = _create(store, timeout_sec=60, now=NOW)

    sweeps: List[datetime] = []
    real_expire_due = waiter_module.expire_due

    def _spy(now: Optional[datetime] = None, *, store: Any = None) -> int:
        sweeps.append(now)
        return real_expire_due(now=now, store=store)

    monkeypatch.setattr(waiter_module, "expire_due", _spy)

    waiter = DecisionWaiter(
        decision.decision_id,
        store=store,
        poll_interval=0.01,
        heartbeat_interval=60.0,
        now_fn=clock,
    )
    waiter.start()
    try:
        clock.advance(61.0)  # one second past the deadline
        resolved = waiter.result(timeout=5.0)
    finally:
        waiter.release()
        waiter.join(timeout=5.0)

    assert resolved.status == DecisionStatus.DEFAULTED.value
    assert resolved.answer == "accept"
    assert sweeps == [NOW + timedelta(seconds=61)], (
        "the decision was not migrated through the service, on the "
        "injected clock"
    )
    assert any(
        record.get("event") == "defaulted" for record in _audit()
    ), "the decision was not migrated through the service"


@pytest.mark.integration
def test_release_ends_the_wait(store: HitlStore) -> None:
    """A released wait ends at once, and says so.

    The release is the way out for a wait nobody is going to answer —
    a plan that is being torn down, a test that is finishing.  It has
    to end the wait *promptly* (not at the next poll), and it has to
    end it distinguishably: a wait that was given up is not a question
    that was answered, and a caller that cannot tell the two apart
    would treat a give-up as a default.
    """
    decision = _create(store, timeout_sec=None)

    waiter = DecisionWaiter(
        decision.decision_id,
        store=store,
        poll_interval=30.0,  # a poll long enough that only the release ends it
        heartbeat_interval=60.0,
    )
    waiter.start()
    try:
        waiter.release()
        waiter.join(timeout=5.0)
        assert not waiter.thread.is_alive(), "the release did not end the wait"
        with pytest.raises(WaiterReleased):
            waiter.result(timeout=1.0)
    finally:
        waiter.release()
        waiter.join(timeout=5.0)


# ---------------------------------------------------------------------------
# 4. The heartbeat that is written down
# ---------------------------------------------------------------------------
#
# The beat is not only a callback.  A wait that says nothing is a wait
# the watchdog kills, and the thing that makes it say something is a
# *fact about the question*: the row carries the instant of the last
# beat, so anybody reading the plan's decisions can tell that a thread
# is still waiting for the answer.


@pytest.mark.integration
def test_the_wait_stamps_the_decision_heartbeat(store: HitlStore) -> None:
    """A wait nobody can see is a wait the watchdog kills.

    The stamp lands on the row itself — ``plan_decisions.heartbeat_at``
    — and it lands *without* the caller having to ask for it.  A
    heartbeat the caller has to remember to send is a heartbeat that is
    missing the first time the caller is busy, and the one time it is
    missing is the one that matters.
    """
    decision = _create(store, timeout_sec=None)

    waiter = DecisionWaiter(
        decision.decision_id,
        store=store,
        poll_interval=0.01,
        heartbeat_interval=0.05,
    )
    waiter.start()
    try:
        assert _wait_until(lambda: _heartbeat_of(store, decision.decision_id)), (
            "the wait never stamped the decision's heartbeat — a wait "
            "that says nothing is a wait the watchdog kills"
        )
    finally:
        waiter.release()
        waiter.join(timeout=5.0)


@pytest.mark.integration
def test_the_heartbeat_keeps_going_while_the_wait_goes_on(
    store: HitlStore,
) -> None:
    """The stamp moves: the wait is alive for as long as it waits.

    One stamp would be enough to say "somebody was waiting once".  What
    the watchdog needs is "somebody is waiting *now*", so the stamp has
    to advance with the wait.
    """
    decision = _create(store, timeout_sec=None)

    waiter = DecisionWaiter(
        decision.decision_id,
        store=store,
        poll_interval=0.01,
        heartbeat_interval=0.05,
    )
    waiter.start()
    try:
        assert _wait_until(lambda: _heartbeat_of(store, decision.decision_id))
        first = _heartbeat_of(store, decision.decision_id)
        assert _wait_until(
            lambda: _heartbeat_of(store, decision.decision_id) != first
        ), "the heartbeat never moved — the wait looks dead after the first beat"
    finally:
        waiter.release()
        waiter.join(timeout=5.0)


@pytest.mark.integration
def test_a_settled_decision_is_not_stamped(store: HitlStore) -> None:
    """The heartbeat is a liveness stamp, not a resolution.

    Only an **open** decision is stamped (``pending`` or ``paused``).
    A question nobody waits for any more has no heartbeat to give, and
    a settled row must not be made to look as if a thread were still
    waiting for an answer that has already come.
    """
    decision = _create(store, timeout_sec=None)
    assert store.answer_if_pending(
        decision.decision_id, "accept", answered_by="tester"
    )

    stamped = store.mark_heartbeat(decision.decision_id)
    assert stamped is False, (
        "a settled decision was stamped — nobody is waiting for it any more"
    )
    assert _heartbeat_of(store, decision.decision_id) is None


@pytest.mark.integration
def test_a_paused_decision_is_still_stamped(store: HitlStore) -> None:
    """A paused question is still a question a human may answer.

    ``paused`` is what a ``strict`` question becomes when its deadline
    passes: the round stopped waiting, but the question was never
    resolved.  It is open, so it is stamped — the resume path has to be
    able to see that somebody is still interested in the answer.
    """
    decision = _create(store, timeout_sec=None)
    assert store.mark_paused(decision.decision_id, note="hitl_strict_timeout")

    assert store.mark_heartbeat(decision.decision_id) is True
    assert _heartbeat_of(store, decision.decision_id) is not None


# ---------------------------------------------------------------------------
# 5. The production wiring
# ---------------------------------------------------------------------------
#
# A waiter nobody calls is a waiter that does not run.  The verification
# round is the caller: it asks a human a question and then waits for
# the answer, and while it waits it has to look alive — the watchdog
# judges a round that produces nothing for 900 seconds to be dead.


@pytest.mark.integration
def test_the_verification_round_waits_for_a_human_and_says_so(
    store: HitlStore,
) -> None:
    """The round that waits says so, where the watchdog looks.

    Two facts are asserted, and they are two different facts.

    The **wait is real**: a pending verification question is not
    something the round walks past — the round blocks on it, and the
    block ends when the human answers.

    And the wait is **visible**: while it blocks, the round's registry
    handle carries the ``hitl_wait`` stage.  That is what the liveness
    probe reads, so a round that is waiting for a human is not a round
    the watchdog reads as dead.
    """
    import server  # noqa: F401 - seeds ``sys.modules['server']``
    import verification_loop
    from sub_agent_registry import sub_agent_registry

    plan_id = "plan-hitl-waiter-wiring"
    decision = _create(
        store,
        plan_id=plan_id,
        source="verification",
        vp_id="VP-023",
        round_number=1,
        timeout_sec=None,
    )

    returned: List[Any] = []

    def _wait() -> None:
        returned.append(
            verification_loop._wait_for_open_verification_decisions(
                plan_id,
                1,
                poll_interval=0.01,
                heartbeat_interval=0.05,
            )
        )

    thread = threading.Thread(target=_wait, name="test-hitl-round-wait", daemon=True)
    thread.start()
    try:
        assert _wait_until(
            lambda: any(
                handle.stage == "hitl_wait"
                for handle in sub_agent_registry.all_handles(plan_id)
            )
        ), (
            "the round never marked the 'hitl_wait' stage — a wait the "
            "watchdog cannot see is a wait the watchdog kills"
        )

        assert store.answer_if_pending(
            decision.decision_id, "accept", answered_by="tester"
        )
        thread.join(timeout=5.0)
    finally:
        thread.join(timeout=5.0)

    assert not thread.is_alive(), "the round did not stop waiting after the answer"
    assert returned == [None], (
        "an answered question is not a reason to stop the round; only a "
        f"paused one is (got {returned!r})"
    )
    assert sub_agent_registry.all_handles(plan_id) == [], (
        "the wait left its handle behind — a handle that outlives its "
        "wait is a handle the watchdog will one day kill"
    )


@pytest.mark.integration
def test_the_verification_round_stops_on_a_paused_question(
    store: HitlStore,
) -> None:
    """A ``strict`` question nobody answered stops the round.

    The pause is not an answer, and the backend may not answer in a
    human's place — so the round does not carry on as if the question
    had been settled.  It reports the paused question and stops, which
    is what the plan's ``pause_state.json`` records.
    """
    import server  # noqa: F401 - seeds ``sys.modules['server']``
    import verification_loop

    plan_id = "plan-hitl-waiter-paused"
    decision = _create(
        store,
        plan_id=plan_id,
        source="verification",
        vp_id="VP-023",
        round_number=1,
        timeout_sec=None,
    )
    assert store.mark_paused(decision.decision_id, note="hitl_strict_timeout")

    returned = verification_loop._wait_for_open_verification_decisions(
        plan_id,
        1,
        poll_interval=0.01,
        heartbeat_interval=0.05,
    )

    assert returned is not None, (
        "the round walked past a paused question — a strict question "
        "nobody answered must stop the round, not be quietly ignored"
    )
    assert returned.decision_id == decision.decision_id
    assert returned.status == DecisionStatus.PAUSED.value
    assert returned.answer is None, (
        "the paused question carries no answer: nobody gave one"
    )


@pytest.mark.integration
def test_a_round_stopped_for_a_human_does_not_look_dead(
    store: HitlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round that stopped for a human must not read as a dead one.

    The watchdog judges a round by two facts: whether its thread is
    alive, and what its status says.  A round that *returned* — because
    it stopped waiting for a human — has a dead thread, so the status
    is the only thing left that can say "this round is not dead".  A
    status of ``running`` would leave the watchdog with nothing but the
    dead thread, and it would fail the plan.
    """
    import server  # noqa: F401 - seeds ``sys.modules['server']``
    import verification_loop

    class _ReturnedThread:
        """A thread that has already returned — the round is over."""

        def is_alive(self) -> bool:
            return False

    plan_id = "plan-hitl-waiter-rest"
    server._verification_state[plan_id] = {
        "plan_id": plan_id,
        "verification_status": "running",
        "verification_round": 1,
        "verification_max_rounds": 3,
        "results": {},
        "repair_tasks": [],
        "started_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "orchestrator": None,
        "stop_reason": None,
        "thread": _ReturnedThread(),
    }
    decision = _create(
        store,
        plan_id=plan_id,
        source="verification",
        vp_id="VP-023",
        round_number=1,
        timeout_sec=None,
    )

    terminal_calls: List[Any] = []
    monkeypatch.setattr(
        server,
        "_persist_verification_terminal",
        lambda *args, **kwargs: terminal_calls.append(args),
    )

    try:
        verification_loop._pause_verification_for_human(plan_id, decision, 1)

        state = server._verification_state[plan_id]
        assert state["verification_status"] == "paused", (
            "a round that stopped for a human is still running as far as "
            "the watchdog is concerned — and a round whose thread has "
            "returned is a dead round to the watchdog"
        )

        verification_loop._lazy_check_verification(plan_id)
        assert terminal_calls == [], (
            "the watchdog failed a plan that was waiting for a human"
        )
    finally:
        server._verification_state.pop(plan_id, None)
