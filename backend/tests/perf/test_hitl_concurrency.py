"""Concurrency and long-wait evidence for the human-in-the-loop subsystem.

Background
----------
The HITL subsystem makes two promises that a serial test suite cannot
see, and both of them are about *what happens when two things happen at
the same time*.

**One answer, one decision.**  A question is asked by one thread and
answered by another — a human, minutes later, possibly from another
process — and two humans may click "accept" in the same second.  The
store's compare-and-set is what makes that safe::

    UPDATE plan_decisions SET ... WHERE decision_id=? AND status='pending'

The predicate is the whole contract: the winner's write moves the row
out of ``pending`` and every other write finds the predicate false.
The existing suite proves the *sequential* half of this — that a second
answer is refused — but a sequential test cannot see the race at all.
The race is where the guarantee is actually spent, so it is exercised
here with twenty threads on one row, over and over.

**A wait that outlives the watchdog.**  The backend's watchdog judges a
thread that has produced nothing for 900 seconds
(``VERIFICATION_WATCHDOG_STALENESS_SECONDS``) to be dead.  A thread
blocked on a human produces nothing *by definition* — it is waiting,
not working — so a wait with no heartbeat is a wait the watchdog kills,
and the question dies with it.  ``hitl/waiter.py`` therefore beats on
its own default schedule (60 seconds today, read from the product
rather than restated here).  Whether that beat is *enough* is not a
question about any single beat: it takes an injected clock crossing the
900-second window to answer it, and the answer has to hold in both
directions — a wait that beats stays alive, and a wait that does not
beat is judged dead.

What is pinned here
-------------------
1. ``test_double_answer_race`` — twenty threads answer one decision at
   the same instant, fifty times over: exactly one answer is taken and
   nineteen are refused, and the row records exactly one transition.
2. ``test_answer_expire_race`` — an answer and an expiry land on the
   same decision: exactly one of them wins, in either order, and the
   decision never carries both an ``answered`` and a ``defaulted``
   write.
3. ``test_heartbeat_liveness_both_ways`` — a wait that beats stays
   alive past the 900-second window; a wait that does not beat is
   judged dead once the window closes.  The reverse case is the one
   that matters: it is the failure the heartbeat exists to prevent.
4. ``test_blocked_task_ids_graph`` — an open question holds the
   schedule shut for the task it is about and for everything
   downstream of it, and answering it opens the gate again by itself.
5. ``test_hung_hook_does_not_delay_creation`` — a destination that
   hangs cannot delay the creation call: the ask comes back inside
   500 ms while the destination is still hanging.

What "real" means here
----------------------
Real SQLite (the per-test state database, opened through the same
``state_machine.db.connection.open`` the product uses, with its WAL and
``busy_timeout`` contract), real threads (the race cases use one
``HitlStore`` *per thread* — a separate connection each — so the
contention is on SQLite's own write lock rather than on a
single-store mutex), and real wall-clock measurement where a bound is
asserted.

Nothing is stubbed out to make a case pass.  Where a case needs a
clock it *injects* one, which is the waiter's own documented seam
(``now_fn``); where a case needs a hang it starts a real subprocess
that really hangs.

Why this file is marked ``perf``
--------------------------------
Two of the cases assert a bound on wall-clock time — the creation call
that must come back inside 500 ms while a destination hangs, and the
heartbeat schedule measured on an injected clock that a real poll loop
has to keep up with.  A bound measured on a runner that is sharing its
CPU with a parallel shard is a bound about the runner, not about the
code.  The marker puts the file in the lane that runs alone
(``pytest tests/ -m "time_sensitive or perf"``), with the 1800-second
budget that lane is the only one to offer.

The plan's brief for this file asks for ``@pytest.mark.time_sensitive``,
and this file would rather carry it: ``backend/pytest.ini`` puts
``-m "not time_sensitive"`` in its ``addopts``, so a file marked that
way is *deselected* by every plain ``pytest`` run — including the
command the plan itself uses to run this file, which would then collect
nothing and prove nothing.  ``perf`` is the marker the two sibling
files in this directory carry, and the repository documents it as
belonging to the same serial lane; the file is therefore collected by
the plan's own command *and* runs alone in CI.

The file's cases must also run *in sequence* within that lane: each one
reads the wall clock, and the double-answer case counts winners across
a barrier, so a case running beside another case's twenty threads would
be measuring a machine that is busier than the one the bound was
written for.

Every thread this file starts is released and joined by the case that
started it, before that case returns.  A thread that outlives its case
is a thread that outlives its store — and, on the next run, a thread
that is still holding a connection to a database that no longer exists.

Test command::

    cd backend && pytest tests/perf/test_hitl_concurrency.py -v
"""

from __future__ import annotations

import json
import statistics
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pytest

#: ``backend/`` — this file lives in ``backend/tests/perf/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config_paths import resolve_plans_dir, resolve_state_db_path  # noqa: E402
from hitl.models import (  # noqa: E402
    Decision,
    DecisionMode,
    DecisionRequest,
    DecisionStatus,
)
from hitl.service import (  # noqa: E402
    answer_decision,
    blocked_task_ids,
    create_decision,
    expire_due,
)
from hitl.store import HitlStore  # noqa: E402
from hitl.waiter import DecisionWaiter  # noqa: E402
from hooks import config_path, write_for_plan  # noqa: E402
from hooks.dispatcher import dispatch  # noqa: E402
from utils.atomic_io import atomic_write_json  # noqa: E402


#: The marker this file carries, and why it is ``perf`` and not
#: ``time_sensitive``.
#:
#: The plan's brief for this file asks for ``@pytest.mark.time_sensitive``
#: — the marker the repository reserves for "asserts on wall-clock time,
#: must run alone on the runner".  But ``backend/pytest.ini`` ships
#: ``-m "not time_sensitive"`` in its ``addopts``, so a
#: ``time_sensitive``-marked file is *deselected* by every plain
#: ``pytest`` invocation, including the command the plan itself uses to
#: run this file.  Marked that way, the file would be invisible to the
#: very command that is supposed to prove it works.
#:
#: ``perf`` is the marker the two sibling files in this directory carry,
#: and the repository's own documentation of it says it is "run by the
#: same serial lane as time_sensitive (``time_sensitive or perf``)",
#: whose ``--timeout=1800`` is "the only budget these can meet on a
#: shared runner".  So the file lands in exactly the lane the brief
#: asks for — the exclusive serial one, alone on the runner — *and* a
#: plain run of the file still collects it.
pytestmark = [pytest.mark.perf]


# ---------------------------------------------------------------------------
# Can this host measure a wall clock at all?
# ---------------------------------------------------------------------------
#
# The load-average gate (``backend/tests/_host_load.py``) skips a
# wall-clock case on a host that is obviously busy -- but it stands down
# on CI, and a host can be slow without being busy (a shared runner
# whose neighbours are quiet, a machine with its CPU throttled, a
# container whose quota is a fraction of the host's).  Such a host
# cannot be judged against a half-second bound: the bound would be
# measuring the machine, not the code.
#
# The gate's own convention (``scripts/run_tests.sh``) already answers
# the question in one number: five samples of a 0.05 s sleep, and a
# median above 0.06 s means the host cannot measure wall-clock.  The
# same probe, with the same numbers, is used here so that the two
# answers cannot drift apart.

#: How long the sleep the host probe measures is.
HOST_PROBE_SLEEP_SEC = 0.05

#: How many samples the median is taken over.
HOST_PROBE_SAMPLES = 5

#: The median sleep a host has to manage for its clock to be trusted.
HOST_PROBE_MAX_SLEEP_SEC = 0.06

#: The environment variable that overrides the threshold -- the same
#: name and the same meaning as the gate's own probe
#: (``scripts/run_tests.sh``), so that an operator who has decided this
#: host *is* measurable can say so in one place.
HOST_PROBE_MAX_SLEEP_ENV = "PDT_HOST_TIMING_PROBE_MAX_SLEEP"


def _host_probe_max_sleep() -> float:
    """The threshold the host probe compares its measurement against.

    The environment override wins when it names a usable number; a broken
    override falls back to the default, because a threshold that cannot
    be read must not silently turn the probe into "always measurable".
    """
    import os

    raw = os.environ.get(HOST_PROBE_MAX_SLEEP_ENV)
    if raw is None:
        return HOST_PROBE_MAX_SLEEP_SEC
    try:
        limit = float(raw)
    except (TypeError, ValueError):
        return HOST_PROBE_MAX_SLEEP_SEC
    if limit <= 0:
        return HOST_PROBE_MAX_SLEEP_SEC
    return limit


def _measure_host_sleep_median(
    sleep_sec: float = HOST_PROBE_SLEEP_SEC,
    samples: int = HOST_PROBE_SAMPLES,
) -> float:
    """The median of *samples* sleeps of *sleep_sec* seconds.

    The median rather than the mean: one unlucky sample (a page fault,
    a scheduler tick) must not decide whether the host is measurable.
    """
    measured = []
    for _ in range(samples):
        started = time.monotonic()
        time.sleep(sleep_sec)
        measured.append(time.monotonic() - started)
    return statistics.median(measured)


def _host_measurement_skip_reason(
    measured: float,
    limit: Optional[float] = None,
) -> Optional[str]:
    """Why the wall-clock bound cannot be asserted on this host, or ``None``.

    ``None`` means the host is measurable: the bound is asserted, and it
    is asserted at the value it always had.  A reason means the host
    cannot be judged against a wall clock, and the case is skipped
    rather than failed -- a skip is allowed, a false failure is not.
    """
    if limit is None:
        limit = _host_probe_max_sleep()
    if measured > limit:
        return (
            f"skipped: the host cannot measure wall-clock "
            f"(sleep({HOST_PROBE_SLEEP_SEC:g})={measured:.3f}s > {limit:g}s)"
        )
    return None


# ---------------------------------------------------------------------------
# The numbers
# ---------------------------------------------------------------------------

#: How many rounds each race case runs.  One round cannot see a race:
#: a window that opens once in a thousand runs is not a window anybody
#: would notice in production, and one sample of it proves nothing.
#: Fifty rounds is the smallest count that turns "it did not happen
#: this time" into "it did not happen in fifty tries".
ROUNDS = 50

#: How many threads answer the same decision at the same instant.  A
#: number large enough that the barrier releases them into a genuine
#: pile-up, and small enough that fifty rounds of it stay a
#: second-scale case.
ANSWERERS = 20

#: The instant the timed cases are anchored at.  A fixed clock is what
#: makes "one timeout in the past" an exact instant instead of a race
#: against the wall clock.
NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)

#: How long a decision waits before it is due.  Chosen so that a
#: decision asked at ``NOW - 2 * TIMEOUT_SEC`` is already overdue
#: without anything having to sleep.
TIMEOUT_SEC = 60

# The heartbeat interval the waiter uses by default.  It is *read*
# from the product rather than restated here: a schedule copied into a
# test is a schedule that stops being true when the product's changes,
# and the case below is about the relationship between that schedule
# and the watchdog's window — not about the number 60.
def _default_heartbeat_interval() -> float:
    """The waiter's default heartbeat interval, in seconds."""
    import inspect

    signature = inspect.signature(DecisionWaiter.__init__)
    return float(signature.parameters["heartbeat_interval"].default)


#: The watchdog's staleness window, read from the production constant
#: rather than restated: a threshold copied into a test is a threshold
#: that stops being true when the product's changes.
def _staleness_seconds() -> float:
    """The watchdog's window, as the product defines it."""
    import server

    return float(server.VERIFICATION_WATCHDOG_STALENESS_SECONDS)


#: The bound the creation call has to come back within while a hook
#: destination is hanging.  Half a second: a create that waited for the
#: hanging destination would take its *timeout* (seconds), not its
#: latency (milliseconds).
CREATE_BOUND_SEC = 0.5

#: The plan ids the cases use.  Each case gets its own, so one case's
#: decisions never appear in another case's listing or audit mirror.
DOUBLE_ANSWER_PLAN = "plan-hitl-concurrency-double"
EXPIRE_PLAN = "plan-hitl-concurrency-expire"
HEARTBEAT_PLAN = "plan-hitl-concurrency-heartbeat"
GRAPH_PLAN = "plan-hitl-concurrency-graph"
HOOK_PLAN = "plan-hitl-concurrency-hook"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock the test moves by hand.

    The waiter reads it through ``now_fn``, so a 960-second wait costs
    the suite a few milliseconds of real time rather than sixteen
    minutes of it.
    """

    def __init__(self, start: datetime = NOW) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        self._now = self._now + timedelta(seconds=seconds)


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> bool:
    """Wait for ``predicate`` to become true, up to ``timeout`` seconds.

    The waiter runs in its own thread, so "the answer has been seen" is
    a fact about another thread: the case waits for it rather than
    assuming it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _join_all(threads: Sequence[threading.Thread], *, timeout: float = 60.0) -> None:
    """Join every thread and refuse to let a stuck one pass unnoticed.

    A case that leaves a thread running has not finished: the thread
    outlives the store it reads, and the next case inherits a machine
    that is busier than the one its bounds were written for.
    """
    for thread in threads:
        thread.join(timeout=timeout)
    alive = [thread.name for thread in threads if thread.is_alive()]
    assert not alive, (
        f"these threads did not finish within {timeout} seconds: {alive!r}"
    )


def _request(plan_id: str, **overrides: Any) -> DecisionRequest:
    """A valid request; ``overrides`` replaces single fields."""
    payload: Dict[str, Any] = {
        "plan_id": plan_id,
        "source": "execution",
        "task_id": "T-1",
        "question": "Ship the change as written?",
        "options": ["accept", "revise"],
        "default_option": "accept",
        "mode": DecisionMode.AUTO.value,
        "timeout_sec": TIMEOUT_SEC,
    }
    payload.update(overrides)
    return DecisionRequest(**payload)


def _plan_dir(plan_id: str) -> Path:
    """The plan's directory — where its ``hitl_config.json`` lives."""
    path = resolve_plans_dir() / plan_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_config(plan_dir: Path, mode: str = "auto", timeout_sec: int = TIMEOUT_SEC) -> None:
    """Write the plan's HITL configuration, with somebody to ask.

    The mode and the deadline are written by the product's own writer,
    so they are the ones the product would store.  The outlet is then
    given a destination the plan's *origin* cannot express:
    ``write_for_plan`` only ever writes the ``cc_connect`` hook a
    session implies, and a question nobody can be asked is
    ``unroutable`` from the start — there is no open question for the
    answer race, the expiry race or the gate to contend over.

    The delivery runs on its own thread, so it never holds the
    creation path this file measures.
    """
    document = write_for_plan(plan_dir, {"mode": mode, "timeout_sec": timeout_sec})
    document["hooks"] = [{"kind": "cmd", "command": "true"}]
    atomic_write_json(config_path(plan_dir), document, reraise=True)


def _option_of(index: int) -> str:
    """The answer racer ``index`` gives: one of the two on the table.

    The service stores the option it is given without second-guessing
    it ("the option is stored as given: which of the offered options it
    is is not this route's business to decide"), so the racers answer
    with the *offered* options — the realistic case — and the case
    pins which of them won by name.
    """
    return "accept" if index % 2 == 0 else "revise"


def _audit_records(plan_id: str) -> List[Dict[str, Any]]:
    """The audit mirror of ``plan_id``, as a list of records."""
    path = resolve_plans_dir() / plan_id / "hitl_decisions.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _events_for(records: Sequence[Dict[str, Any]], decision_id: str) -> List[str]:
    """The event names the mirror holds for one decision."""
    return [
        record["event"]
        for record in records
        if record.get("decision_id") == decision_id
    ]


def _stamp_heartbeat(store: HitlStore, decision_id: str, moment: datetime) -> None:
    """Write the wait's sign of life into the row.

    The waiter deliberately does *not* write the heartbeat itself: "the
    beat is a callback, not a write, because *what* a heartbeat means
    belongs to the caller that owns the thread" (``hitl/waiter.py``).
    This helper is that caller — the thread owner stamping
    ``heartbeat_at`` — and it is the same column the schema documents
    as "the last sign of life from the process".
    """
    store._conn.execute(  # noqa: SLF001 - the store keeps its connection private
        "UPDATE plan_decisions SET heartbeat_at = ? WHERE decision_id = ?",
        (moment.isoformat(), decision_id),
    )


def _judged_dead(
    now: datetime,
    last_signal: Optional[datetime],
    wait_started: datetime,
) -> bool:
    """The watchdog's own rule, applied to a wait.

    The watchdog judges a thread dead when it has produced nothing for
    ``VERIFICATION_WATCHDOG_STALENESS_SECONDS`` — the same shape as the
    staleness check in ``verification_loop.py``, which compares the
    clock against the age of the freshest signal.  A wait's freshest
    signal is its last heartbeat; a wait that has never beaten has no
    signal at all, and the wait's own start is then the instant the
    silence began.

    The verdict belongs to the watchdog, not to the waiter: the waiter
    never gives up on a question, so a wait that is "dead" by this rule
    is a wait the watchdog *would* kill, and that is exactly the
    failure the heartbeat exists to prevent.
    """
    reference = last_signal if last_signal is not None else wait_started
    return (now - reference).total_seconds() > _staleness_seconds()


def _signal_of(store: HitlStore, decision_id: str) -> Optional[datetime]:
    """The row's last sign of life, read back through the store."""
    decision = store.get(decision_id)
    assert decision is not None, f"decision {decision_id!r} is not stored"
    raw = decision.heartbeat_at
    if not raw:
        return None
    moment = datetime.fromisoformat(str(raw))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


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


@pytest.fixture
def stores() -> List[HitlStore]:
    """One store per racing thread — a separate connection each.

    Separate connections, not one shared store: the store's own
    ``self._lock`` would serialise the racers before they ever reached
    SQLite, and the race that matters is the one SQLite's write lock
    and ``busy_timeout`` have to survive — the cross-actor case the
    store's own docstring names ("separate ``HitlStore`` instances (and
    separate processes) are serialised by SQLite's own write lock").
    """
    opened = [HitlStore(resolve_state_db_path()) for _ in range(ANSWERERS)]
    try:
        yield opened
    finally:
        for one in opened:
            one.close()


# ---------------------------------------------------------------------------
# 1. Twenty answers, one decision
# ---------------------------------------------------------------------------


def test_double_answer_race(store: HitlStore, stores: List[HitlStore]) -> None:
    """Twenty answers to one decision: one is taken, nineteen are refused.

    The contract is the compare-and-set's predicate —
    ``WHERE decision_id=? AND status='pending'`` — so exactly one of
    the racers may move the row and the other nineteen must find the
    predicate already false.  This is asserted fifty times over, because
    the failure this guards against is not "the second answer is
    refused" (the sequential suite already proves that) but "two
    answers are *taken*", which only shows up when the two land in the
    same instant.

    Each racer gets its own connection, so the contention is on
    SQLite's write lock rather than on one store's mutex.

    The row is checked as well as the responses: a decision that ended
    up with one stored answer but two transitions would mean the audit
    trail told a different story from the row, and an audit trail that
    disagrees with the truth is worse than no audit trail.
    """
    plan_id = DOUBLE_ANSWER_PLAN
    plan_dir = _plan_dir(plan_id)
    _write_config(plan_dir)

    decision_ids: List[str] = []

    for round_index in range(ROUNDS):
        decision = create_decision(_request(plan_id), plan_dir, store=store)
        decision_ids.append(decision.decision_id)

        barrier = threading.Barrier(ANSWERERS)
        results: List[Optional[Tuple[str, int]]] = [None] * ANSWERERS
        errors: List[BaseException] = []

        def racer(index: int) -> None:
            try:
                barrier.wait(timeout=30)
                results[index] = answer_decision(
                    decision.decision_id,
                    _option_of(index),
                    f"operator-{index}",
                    "",
                    plan_id=plan_id,
                    store=stores[index],
                )
            except BaseException as exc:  # noqa: BLE001 - reported, not raised
                errors.append(exc)

        threads = [
            threading.Thread(
                target=racer,
                args=(index,),
                name=f"hitl-answer-{round_index}-{index}",
                daemon=True,
            )
            for index in range(ANSWERERS)
        ]
        for thread in threads:
            thread.start()
        _join_all(threads)

        assert not errors, (
            f"round {round_index}: an answerer raised {errors!r}"
        )
        codes = [result[1] for result in results if result is not None]
        assert len(codes) == ANSWERERS, (
            f"round {round_index}: only {len(codes)} of {ANSWERERS} racers "
            f"reported a result"
        )
        accepted = codes.count(200)
        refused = codes.count(409)
        assert (accepted, refused) == (1, ANSWERERS - 1), (
            f"round {round_index}: {accepted} answers were taken and "
            f"{refused} were refused; exactly one may be taken"
        )

        stored = store.get(decision.decision_id)
        assert stored is not None
        assert stored.status == DecisionStatus.ANSWERED.value, (
            f"round {round_index}: the decision ended {stored.status!r}"
        )
        assert stored.audit_seq == 1, (
            f"round {round_index}: the decision carries {stored.audit_seq} "
            f"transitions; exactly one may have been applied"
        )
        # The stored answer is the *winner's* answer: the racer that was
        # told "taken" is the one whose name and option are on the row.
        winner = next(
            index for index, result in enumerate(results) if result[1] == 200
        )
        assert stored.answered_by == f"operator-{winner}", (
            f"round {round_index}: the row names {stored.answered_by!r} as "
            f"the answerer, but racer {winner} was the one that was told "
            f"its answer was taken"
        )
        assert stored.answer == _option_of(winner), (
            f"round {round_index}: the stored answer {stored.answer!r} is "
            f"not the answer racer {winner} gave"
        )

    records = _audit_records(plan_id)
    for decision_id in decision_ids:
        events = _events_for(records, decision_id)
        assert events == ["created", "answered"], (
            f"decision {decision_id!r} has the audit trail {events!r}; a "
            f"decision that was answered once has exactly one answering "
            f"event"
        )
    assert not [
        record
        for record in records
        if record.get("event") == "defaulted"
    ], "no decision in this case may have been defaulted — nobody's answer won"


# ---------------------------------------------------------------------------
# 2. The answer and the expiry, at the same instant
# ---------------------------------------------------------------------------


def test_answer_expire_race(store: HitlStore, stores: List[HitlStore]) -> None:
    """An answer and an expiry on one decision: exactly one of them wins.

    Two actors want to resolve the same question: the human who
    answered it, and the sweep that resolves an ``auto`` decision whose
    deadline has passed.  Both go through the same compare-and-set, so
    the loser writes nothing — which is the property that keeps the
    decision from carrying *two* resolutions (an ``answered`` write
    *and* a ``defaulted`` one), each of which would be a fact about the
    same question that contradicts the other.

    The race is run fifty times, and then run again in both *orders*,
    because "exactly one wins" and "the answer wins when it arrives
    first" are two different claims and the second one is the one a
    caller can rely on.
    """
    plan_id = EXPIRE_PLAN
    plan_dir = _plan_dir(plan_id)
    _write_config(plan_dir)

    # Asked two timeouts in the past, so the deadline is already behind
    # the clock the sweep reads: the expiry is due now, without
    # anything having to wait for it.
    asked_at = NOW - timedelta(seconds=2 * TIMEOUT_SEC)

    decision_ids: List[str] = []

    for round_index in range(ROUNDS):
        decision = create_decision(
            _request(plan_id), plan_dir, store=store, now=asked_at
        )
        decision_ids.append(decision.decision_id)

        barrier = threading.Barrier(2)
        outcome: Dict[str, Any] = {"answer": None, "expired": None}
        errors: List[BaseException] = []

        def answerer() -> None:
            try:
                barrier.wait(timeout=30)
                outcome["answer"] = answer_decision(
                    decision.decision_id,
                    "accept",
                    "operator",
                    "",
                    plan_id=plan_id,
                    store=stores[0],
                )
            except BaseException as exc:  # noqa: BLE001 - reported, not raised
                errors.append(exc)

        def sweeper() -> None:
            try:
                barrier.wait(timeout=30)
                outcome["expired"] = expire_due(now=NOW, store=stores[1])
            except BaseException as exc:  # noqa: BLE001 - reported, not raised
                errors.append(exc)

        threads = [
            threading.Thread(
                target=answerer,
                name=f"hitl-answer-{round_index}",
                daemon=True,
            ),
            threading.Thread(
                target=sweeper,
                name=f"hitl-expire-{round_index}",
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        _join_all(threads)

        assert not errors, f"round {round_index}: a racer raised {errors!r}"

        answered = outcome["answer"]
        expired = outcome["expired"]
        assert answered is not None and expired is not None
        answer_won = answered[1] == 200
        expiry_won = expired == 1
        assert answer_won != expiry_won, (
            f"round {round_index}: the answer reported {answered!r} and the "
            f"expiry reported {expired!r}; exactly one of them may resolve "
            f"the decision"
        )

        stored = store.get(decision.decision_id)
        assert stored is not None
        assert stored.status in (
            DecisionStatus.ANSWERED.value,
            DecisionStatus.DEFAULTED.value,
        ), f"round {round_index}: the decision ended {stored.status!r}"
        assert stored.audit_seq == 1, (
            f"round {round_index}: the decision carries {stored.audit_seq} "
            f"transitions; exactly one may have been applied"
        )

    records = _audit_records(plan_id)
    for decision_id in decision_ids:
        events = _events_for(records, decision_id)
        assert events[0] == "created"
        assert len(events) == 2, (
            f"decision {decision_id!r} has the audit trail {events!r}; a "
            f"decision that was resolved once has exactly one resolving "
            f"event"
        )
        assert events[1] in ("answered", "defaulted"), (
            f"decision {decision_id!r} has the audit trail {events!r}"
        )
        assert "answered" not in events[1:] or "defaulted" not in events[1:], (
            f"decision {decision_id!r} was both answered and defaulted "
            f"({events!r}) — two resolutions of one question"
        )

    # The two orders, pinned deterministically: the answer that arrives
    # first is the answer that counts, and the sweep that arrives first
    # is the sweep that counts.
    first = create_decision(_request(plan_id), plan_dir, store=store, now=asked_at)
    assert answer_decision(
        first.decision_id, "accept", "operator", "", plan_id=plan_id, store=store
    ) == (DecisionStatus.ANSWERED.value, 200)
    assert expire_due(now=NOW, store=store) == 0, (
        "a decision that was answered must not be defaulted afterwards"
    )
    assert store.get(first.decision_id).status == DecisionStatus.ANSWERED.value

    second = create_decision(_request(plan_id), plan_dir, store=store, now=asked_at)
    assert expire_due(now=NOW, store=store) == 1
    assert answer_decision(
        second.decision_id, "accept", "operator", "", plan_id=plan_id, store=store
    ) == (DecisionStatus.DEFAULTED.value, 409), (
        "a decision that was defaulted must refuse a later answer"
    )
    assert store.get(second.decision_id).status == DecisionStatus.DEFAULTED.value


# ---------------------------------------------------------------------------
# 3. The heartbeat, both ways
# ---------------------------------------------------------------------------


def test_heartbeat_liveness_both_ways(store: HitlStore) -> None:
    """A wait that beats stays alive; a wait that does not beat is killed.

    The watchdog judges a thread that has produced nothing for
    ``VERIFICATION_WATCHDOG_STALENESS_SECONDS`` to be dead.  A thread
    waiting for a human produces nothing by definition, so the waiter
    beats — on its own default schedule — to keep the wait inside that
    window.  Both halves of that claim are asserted here, and the
    *reverse* half is the one that matters: it is the failure the
    heartbeat exists to prevent, and a test that only asserts the happy
    direction would pass just as well with the heartbeat removed.

    The schedule under test is the *product's*: the waiter is built
    without a ``heartbeat_interval``, so it beats on the interval
    ``hitl/waiter.py`` defaults to, and the case asserts that this
    default fits inside the watchdog's window.  A schedule that grew
    past the window would keep a wait alive on paper and let the
    watchdog kill it in practice.

    The positive case: the clock is moved past the window in
    one-interval steps.  A beat lands on every step, so the sign of
    life never ages past the beat interval — far inside the window —
    and the wait is never judged dead.

    The negative case: the same wait with no heartbeat at all.  Nothing
    is ever produced, so the silence is measured from the start of the
    wait: the wait is still alive just under the window and is judged
    dead once the injected clock crosses it.

    The waiter itself never gives up — it does not resolve questions
    and it does not abandon them — so "dead" here is the *watchdog's*
    verdict about the wait, not the waiter's own ending.
    """
    staleness = _staleness_seconds()
    assert staleness == 900.0, (
        f"the watchdog's staleness window is {staleness!r}; this case is "
        f"written against the documented 900 seconds"
    )
    interval = _default_heartbeat_interval()
    assert interval < staleness, (
        f"the waiter beats every {interval} seconds, which is not inside "
        f"the watchdog's {staleness}-second window: a wait on this "
        f"schedule would be killed before its next beat"
    )

    plan_id = HEARTBEAT_PLAN

    # -- the positive direction: a wait that beats ------------------------
    plan_dir = _plan_dir(plan_id)
    _write_config(plan_dir)

    clock = FakeClock()
    beats: List[datetime] = []

    # The waiter's own store is used by the worker thread alone; the
    # reader and the heartbeat writer each get their own connection, so
    # no two threads ever share one.
    waiter_store = HitlStore(resolve_state_db_path())
    heartbeat_store = HitlStore(resolve_state_db_path())
    try:
        request = _request(plan_id, timeout_sec=None)
        decision = Decision.from_request(request, now=clock())
        waiter_store.insert(decision)

        def beat() -> None:
            moment = clock()
            # The stamp lands *before* the beat is counted.  The case
            # waits for the count and then reads the row, and a count
            # that could outrun its own stamp would turn that read into
            # a race instead of a fact.
            _stamp_heartbeat(heartbeat_store, decision.decision_id, moment)
            beats.append(moment)

        waiter = DecisionWaiter(
            decision.decision_id,
            store=waiter_store,
            on_heartbeat=beat,
            poll_interval=0.01,
            now_fn=clock,
        )
        waiter.start()
        try:
            started = clock()
            steps = int(staleness // interval) + 1
            for step in range(1, steps + 1):
                # The waiter's schedule is anchored at the start of the
                # wait, so the multiples of the interval are exactly the
                # instants a beat is owed.  Step one lands on the first
                # multiple; every later step lands on the next.
                clock.advance(interval if step == 1 else 1.0)
                expected = clock()

                def landed() -> bool:
                    signal = _signal_of(store, decision.decision_id)
                    return signal is not None and signal >= expected

                assert _wait_until(
                    lambda: len(beats) >= step and landed()
                ), f"heartbeat #{step} never arrived"
                # Now let the sign of life age to the worst it can get
                # before the next beat is owed: one interval minus a
                # second.  A wait that beats is a wait whose sign of
                # life never reaches the watchdog's window.
                clock.advance(interval - 1.0)
                signal = _signal_of(store, decision.decision_id)
                assert signal is not None, (
                    "the wait beat, but the row carries no sign of life"
                )
                age = (clock() - signal).total_seconds()
                assert age <= interval, (
                    f"the sign of life is {age} seconds old; a wait that "
                    f"beats every {interval} seconds must "
                    f"never be older than one beat interval"
                )
                assert not _judged_dead(clock(), signal, started), (
                    f"a wait that beats was judged dead after "
                    f"{(clock() - started).total_seconds()} seconds"
                )

            assert (clock() - started).total_seconds() > staleness, (
                "this case has to cross the watchdog's window to mean "
                "anything"
            )
            assert waiter.finished is False, (
                "the wait ended although nobody answered — a wait that "
                "beats is a wait that is still waiting"
            )
            assert len(beats) >= steps, (
                f"only {len(beats)} heartbeats in {steps} beat intervals"
            )
        finally:
            waiter.release()
            waiter.join(timeout=10.0)
            assert not waiter.thread.is_alive(), (
                "the waiter's thread outlived the wait"
            )
    finally:
        waiter_store.close()
        heartbeat_store.close()

    # -- the negative direction: a wait that does not beat ----------------
    silent_clock = FakeClock()
    silent_store = HitlStore(resolve_state_db_path())
    try:
        request = _request(plan_id, timeout_sec=None)
        silent = Decision.from_request(request, now=silent_clock())
        silent_store.insert(silent)

        silent_waiter = DecisionWaiter(
            silent.decision_id,
            store=silent_store,
            on_heartbeat=None,
            poll_interval=0.01,
            now_fn=silent_clock,
        )
        silent_waiter.start()
        try:
            started = silent_clock()
            # Just inside the window: the watchdog has not yet had
            # grounds to call the wait dead.
            silent_clock.advance(staleness - 1.0)
            assert _signal_of(store, silent.decision_id) is None, (
                "a wait with no heartbeat must never produce a sign of life"
            )
            assert not _judged_dead(silent_clock(), None, started), (
                "a wait that has been silent for less than the watchdog's "
                "window must not be judged dead yet"
            )

            # Across the window: now it is.
            silent_clock.advance(2.0)
            assert _judged_dead(silent_clock(), None, started), (
                "a wait that produced nothing for longer than the "
                "watchdog's window must be judged dead — this is the "
                "failure the heartbeat exists to prevent"
            )
            assert silent_waiter.finished is False, (
                "the waiter gave up on a question it was never allowed to "
                "resolve"
            )
        finally:
            silent_waiter.release()
            silent_waiter.join(timeout=10.0)
            assert not silent_waiter.thread.is_alive(), (
                "the waiter's thread outlived the wait"
            )
    finally:
        silent_store.close()


# ---------------------------------------------------------------------------
# 4. The scheduling gate
# ---------------------------------------------------------------------------


def test_blocked_task_ids_graph(store: HitlStore) -> None:
    """An open question holds the task it is about and everything below it.

    While a question is open, the work it is about must not run: the
    task would execute on a premise the human has not confirmed yet,
    and so would everything downstream of it.  The gate is *computed*,
    never stored — no task status changes — so answering the question
    opens it again by itself, with nothing to undo.

    The graph is read from both places it is written: the plan's own
    ``tasks.json`` and the runtime ``plan_tasks`` table.  Both are
    populated here, so the case pins the merge rather than one source.
    """
    plan_id = GRAPH_PLAN
    plan_dir = _plan_dir(plan_id)
    _write_config(plan_dir)

    # A chain: A has no dependency, B depends on A, C depends on B.  D
    # is unrelated to all of them.
    tasks = [
        {"id": "T-A", "depends_on": []},
        {"id": "T-B", "depends_on": ["T-A"]},
        {"id": "T-C", "depends_on": ["T-B"]},
        {"id": "T-D", "depends_on": []},
    ]
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": tasks}), encoding="utf-8"
    )
    for task in tasks:
        store._conn.execute(  # noqa: SLF001 - the store keeps its connection private
            "INSERT INTO plan_tasks (plan_id, task_id, depends_on, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (
                plan_id,
                task["id"],
                json.dumps(task["depends_on"]) if task["depends_on"] else None,
                NOW.isoformat(),
            ),
        )

    decision = create_decision(
        _request(plan_id, task_id="T-A"), plan_dir, store=store
    )

    blocked = blocked_task_ids(plan_id, store=store)
    assert blocked == {"T-A", "T-B", "T-C"}, (
        f"an open question about T-A must hold T-A and everything "
        f"downstream of it; blocked={sorted(blocked)!r}"
    )
    assert "T-D" not in blocked, (
        "a task that is not downstream of the open question must keep "
        "running"
    )

    # The gate is computed, never stored: no task status was touched.
    rows = store._conn.execute(  # noqa: SLF001
        "SELECT task_id, status FROM plan_tasks WHERE plan_id = ?", (plan_id,)
    ).fetchall()
    assert all(row["status"] is None for row in rows), (
        "the gate must not change any task's status — it is computed"
    )

    # A question about an unrelated task holds only that task.
    other = create_decision(
        _request(plan_id, task_id="T-D"), plan_dir, store=store
    )
    blocked_other = blocked_task_ids(plan_id, store=store)
    assert blocked_other == {"T-A", "T-B", "T-C", "T-D"}, (
        f"two open questions hold the union of their downstream sets; "
        f"blocked={sorted(blocked_other)!r}"
    )

    # Answering the questions opens the gate again by itself.
    answer_decision(
        decision.decision_id, "accept", "operator", "", plan_id=plan_id, store=store
    )
    answer_decision(
        other.decision_id, "accept", "operator", "", plan_id=plan_id, store=store
    )
    assert blocked_task_ids(plan_id, store=store) == set(), (
        "a plan with no open question schedules exactly what it scheduled "
        "before — nothing is stored, so nothing has to be undone"
    )


# ---------------------------------------------------------------------------
# 5. The hung destination
# ---------------------------------------------------------------------------


def test_hung_hook_does_not_delay_creation() -> None:
    """A destination that hangs cannot delay the creation call.

    The dispatcher says it plainly: "the call blocks until every
    destination has been attempted, so the call is bounded by the
    slowest destination's timeout — not by the sum of them, because
    they run at the same time.  A caller that must answer a request
    quickly (a route handler, a decision creation) should therefore
    run this on a worker thread rather than on the request's own
    thread."

    So the shape under test is the one the architecture prescribes: the
    dispatch runs on a worker thread, and the creation call — the HTTP
    request a human is waiting on — comes back while the hanging
    destination is still hanging.  The bound is half a second, and the
    hanging destination's own timeout is several times that, so a
    creation that *waited* for it could not meet the bound.

    The destination is a real subprocess that really hangs: a ``sleep``
    that is abandoned on its own timeout, and the report says so.

    The bound is a wall-clock bound, so it is asserted only on a host
    that can measure a wall clock at all: a host whose own 0.05 s sleep
    costs a fifth of a second cannot be judged against half a second,
    and failing it there would be a statement about the machine rather
    than about the code.  On such a host the case is **skipped** -- the
    bound itself is not weakened, and it is asserted unchanged wherever
    the host is measurable.
    """
    reason = _host_measurement_skip_reason(_measure_host_sleep_median())
    if reason is not None:
        pytest.skip(reason)

    from fastapi.testclient import TestClient

    import server

    plan_id = HOOK_PLAN
    plan_dir = _plan_dir(plan_id)

    # The destination hangs for far longer than the timeout it is given,
    # so the dispatcher has to abandon it rather than wait it out.
    hook_timeout_sec = 2
    (plan_dir / "hitl_config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "auto",
                "timeout_sec": TIMEOUT_SEC,
                "hooks": [
                    {
                        "kind": "cmd",
                        "command": "sleep 30",
                        "timeout_sec": hook_timeout_sec,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    reports: List[Any] = []
    worker = threading.Thread(
        target=lambda: reports.append(
            dispatch("hitl_decision_created", {"decision_id": "d-hung"}, plan_dir)
        ),
        name="hitl-hook-dispatch",
        daemon=True,
    )
    worker.start()
    try:
        # Give the destination time to be launched and hang.
        time.sleep(0.2)
        assert worker.is_alive(), (
            "the dispatch finished before the destination could hang — the "
            "case would not be testing anything"
        )

        client = TestClient(server.app)
        started = time.perf_counter()
        response = client.post(
            f"/api/hitl/{plan_id}/decisions",
            json={
                "question": "Ship the change as written?",
                "options": ["accept", "revise"],
                "default_option": "accept",
            },
        )
        elapsed = time.perf_counter() - started

        assert response.status_code == 200, response.text
        assert elapsed < CREATE_BOUND_SEC, (
            f"the creation call took {elapsed:.3f} seconds while a "
            f"destination was hanging; it must come back inside "
            f"{CREATE_BOUND_SEC} seconds"
        )
        assert worker.is_alive(), (
            "the dispatch had already finished when the creation call "
            "returned — the creation did not wait for the hanging "
            "destination, but neither did it overlap it"
        )
    finally:
        _join_all([worker], timeout=30.0)

    assert reports, "the dispatch reported nothing"
    report = reports[0]
    assert report.timed_out == 1, (
        f"the hanging destination was not abandoned on its own clock: "
        f"{report!r}"
    )
    assert report.delivered == 0, (
        f"a destination that hangs delivers nothing: {report!r}"
    )


# ---------------------------------------------------------------------------
# 6. Whether this host can be judged against a wall clock
# ---------------------------------------------------------------------------


def test_the_wall_clock_bound_is_kept_on_a_measurable_host() -> None:
    """A host that can measure keeps the bound; the bound does not move.

    The half-second bound is the contract this file exists to pin, and
    it is asserted at exactly that value wherever the host is able to
    measure.  What the probe decides is only *whether the host is able
    to measure* -- never how long the code is allowed to take.
    """
    assert _host_measurement_skip_reason(0.05) is None, (
        "a host whose 0.05s sleep costs 0.05s is measurable; the "
        "wall-clock bound must still be asserted on it"
    )
    reason = _host_measurement_skip_reason(0.2)
    assert reason is not None, (
        "a host whose 0.05s sleep costs 0.2s cannot be judged against a "
        "half-second bound"
    )
    assert "cannot measure wall-clock" in reason, reason


def test_the_wall_clock_case_skips_itself_on_a_host_that_cannot_measure(
    monkeypatch,
) -> None:
    """The case skips -- it does not fail -- on a host it cannot measure.

    The load-average gate already skips this file when the machine is
    obviously busy; this is the other half, the host that is slow
    without being busy.  A **skip** is allowed by the gate's own
    contract; a failure there would be a statement about the machine.
    """
    # The module is patched *by the module object itself*, not by a
    # dotted name: the suite imports this file as
    # ``backend.tests.perf.test_hitl_concurrency`` (every directory up
    # to the repository root is a package), while the dotted name below
    # would import a **second** copy of the file under
    # ``tests.perf.test_hitl_concurrency`` — and a copy that nothing
    # calls.  The case would then measure the real host, find it
    # measurable, and never skip: the skip it is here to prove would
    # never happen.
    monkeypatch.setattr(
        sys.modules[__name__],
        "_measure_host_sleep_median",
        lambda *args, **kwargs: 0.5,
    )

    with pytest.raises(pytest.skip.Exception):
        test_hung_hook_does_not_delay_creation()
