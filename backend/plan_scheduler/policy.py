"""backend/plan_scheduler.policy — the scheduler's decisions, as pure functions.

Background
----------
The scheduler makes three kinds of decision: **may this plan start
now**, **who gets interrupted when a slot has to be freed**, and **what
has to go when the scheduler is running more plans than it has slots**.

All three are asked over and over, from the tick that admits a plan,
from the panel that explains to the operator why a plan is not running,
and from the dry run that records what the live engine would have
done.  If each of them answered on its own, the panel would explain one
thing while the engine did another — and the operator's only way to
find out which was which would be to watch the wrong plan get
interrupted.

So the three decisions are **pure functions of a snapshot**.  A snapshot
is plain data — what the scheduler could read at one instant — and the
functions below never reach past it: no I/O, no clock of their own, no
shared state.  The same snapshot always produces the same answer, in
this process and in the next one.

**One vocabulary for every reason.**  Whatever the decision is, the
reason attached to it comes from :data:`REASONS`, a closed set of eight
words.  The panel renders them, the dry run records them and the API
publishes them; none of them composes a sentence of its own.  A reason
outside the set is a bug in the policy, not a new situation to be
described in a new word.

**The eviction candidate set is shared.**  The two triggers that may
interrupt a running plan — an operator asking for a plan to start now,
and the tick finding more plans running than there are slots — pick
their victim from the same candidates, by the same rule: a plan that
has been running for at least ``M`` seconds, and among those the
*youngest* one, the plan that took its slot most recently.  Evicting
the oldest would interrupt the plan that is closest to finishing; the
youngest is the one least likely to be wrapping up, and the ``M``
threshold keeps the default 600 seconds of protection intact.

**A plan that is too young is not a candidate at all**, and an empty
candidate set is an answer rather than an error:
:func:`select_victim` returns ``None`` and :func:`converge` stops.

The snapshot the functions read
-------------------------------

A snapshot is a mapping.  Only these keys are read, and each of them
has a default, so a caller may leave out anything it does not know::

    {
        "queued": [                      # the queue, ordered by queue_key
            {"plan_id": "p1",
             "due": True,               # next_run_at has arrived
             "workspace_busy": False,   # another plan holds the workspace
             "paused": False,           # the operator paused it
             "hitl_pending": False},    # it is parked awaiting an answer
        ],
        "used": 4,                      # plans currently holding a slot
        "capacity": 4,                  # slots the scheduler has
        "running": [                    # the plans holding a slot
            {"plan_id": "r1",
             "elapsed": 600,            # seconds since the slot was taken
             "acquired_at": 100},        # when the slot was taken
        ],
        "allow_evict": True,            # may a running plan be interrupted?
        "min_runtime_seconds": 600,     # M — the runtime a plan is protected for
    }
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import DEFAULT_MIN_RUNTIME_SECONDS

__all__ = [
    "REASONS",
    "Snapshot",
    "admission_verdict",
    "select_victim",
    "converge",
]

#: The closed vocabulary of reasons.  Every decision the policy makes
#: carries one of these words, and the panel, the dry run and the API
#: all read the same eight.  It is a *set* on purpose: there is no
#: order to it, because a reason is a label and not a step.
REASONS = frozenset(
    {
        "running",
        "waiting_for_slot",
        "blocked_by_capacity",
        "workspace_busy",
        "due_later",
        "hitl_pending",
        "paused",
        "over_capacity",
    }
)

#: A snapshot is plain data: what the scheduler could read at one
#: instant.  The policy never writes to it.
Snapshot = Dict[str, Any]


# ---------------------------------------------------------------------------
# Reading a snapshot
# ---------------------------------------------------------------------------


def _value(row: Any, name: str, default: Any) -> Any:
    """Read ``name`` out of ``row``, falling back to ``default``.

    A row is read the same way whatever shape it has: a mapping
    answers ``get``, anything else answers ``getattr``.  A value that is
    present but ``NULL`` reads as the default — for the policy's
    purposes an unset field and an absent one mean the same thing.
    """
    getter = getattr(row, "get", None)
    if callable(getter):
        value = getter(name, default)
    else:
        value = getattr(row, name, default)
    return default if value is None else value


def _number(value: Any, default: float) -> float:
    """Return ``value`` as a number, or ``default`` when it is not one.

    A duration the policy cannot read is a duration it cannot compare,
    and guessing would be worse than saying "not known": the plan is
    then treated as young, which protects it rather than evicting it.
    """
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _plan_id(entry: Any) -> str:
    """Return the plan id of a queued or running entry."""
    return str(_value(entry, "plan_id", ""))


def _elapsed(entry: Any) -> float:
    """Return how long the entry has been running, in seconds."""
    return _number(_value(entry, "elapsed", 0), 0.0)


def _acquired_at(entry: Any) -> float:
    """Return when the entry took its slot, as a number."""
    return _number(_value(entry, "acquired_at", 0), 0.0)


def _queued_entries(snapshot: Snapshot) -> List[Any]:
    """Return the snapshot's queued plans, in the order they were given."""
    entries = _value(snapshot, "queued", [])
    try:
        return list(entries)
    except TypeError:
        return []


def _running_entries(snapshot: Snapshot) -> List[Any]:
    """Return the snapshot's running plans, in the order they were given."""
    entries = _value(snapshot, "running", [])
    try:
        return list(entries)
    except TypeError:
        return []


def _min_runtime(snapshot: Snapshot) -> float:
    """Return the snapshot's ``M`` — the runtime a plan is protected for.

    The threshold belongs to the configuration, and the snapshot carries
    the value the tick read from it.  A snapshot that does not carry
    one falls back to the configured default, so a caller that forgets
    the key gets the same protection the scheduler runs with.
    """
    return _number(
        _value(snapshot, "min_runtime_seconds", DEFAULT_MIN_RUNTIME_SECONDS),
        DEFAULT_MIN_RUNTIME_SECONDS,
    )


def _used(snapshot: Snapshot) -> int:
    """Return how many plans hold a slot, according to the ledger."""
    running = _running_entries(snapshot)
    return int(_number(_value(snapshot, "used", len(running)), float(len(running))))


def _capacity(snapshot: Snapshot) -> int:
    """Return how many slots the scheduler has."""
    return int(_number(_value(snapshot, "capacity", 0), 0.0))


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def admission_verdict(snapshot: Snapshot) -> str:
    """Return why the next queued plan is or is not running.

    The decision is made on the **head of the queue** — the plan the
    next admission is about — and follows the order the architecture
    pins: the plan has to be queued, it has to be due, its workspace
    has to be free, and then there has to be a slot for it.

    The reasons, in the order they are decided:

    * ``paused`` — the operator stopped this plan; it is not waiting
      for anything and will not start on its own.
    * ``hitl_pending`` — it is parked awaiting an answer.  It is not
      waiting for a slot, and saying so would send the operator
      looking in the wrong place.
    * ``due_later`` — it has not come due yet.
    * ``workspace_busy`` — another plan holds the workspace; only one
      plan at a time may work in it.
    * ``over_capacity`` — the scheduler is running more plans than it
      has slots, so it has to converge before anything new starts.
    * ``running`` — it may run: either a slot is free, or the snapshot
      allows an eviction and there is an eligible victim to interrupt.
    * ``waiting_for_slot`` — every slot is taken and nothing may be
      interrupted, so it waits for one to come free.
    * ``blocked_by_capacity`` — every slot is taken, an eviction was
      allowed, but nobody is old enough to be interrupted.  The plan
      stays at the head of the queue.

    The reasons are drawn from :data:`REASONS` and from nowhere else.
    """
    queued = _queued_entries(snapshot)
    if not queued:
        # Nothing is waiting: the plans that are running keep running.
        return "running"

    head = queued[0]
    if _value(head, "paused", False):
        return "paused"
    if _value(head, "hitl_pending", False):
        return "hitl_pending"
    if not _value(head, "due", True):
        return "due_later"
    if _value(head, "workspace_busy", False):
        return "workspace_busy"

    used = _used(snapshot)
    capacity = _capacity(snapshot)
    if used > capacity:
        # The scheduler is over its own limit; it converges first.
        return "over_capacity"
    if used < capacity:
        # A slot is free — the plan may start.
        return "running"

    if not _value(snapshot, "allow_evict", False):
        # Every slot is taken and nobody may be interrupted.
        return "waiting_for_slot"
    if select_victim(snapshot, _min_runtime(snapshot)) is None:
        # Every slot is taken and there is nobody old enough to go.
        return "blocked_by_capacity"
    return "running"


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


def _youngest(entries: Sequence[Any]) -> Optional[Any]:
    """Return the entry that took its slot most recently.

    The youngest is the one with the **latest** ``acquired_at`` — the
    plan that started last, and therefore the one least likely to be
    wrapping up.  Two plans that took their slot at the same moment are
    told apart by ``plan_id`` in dictionary order, so the answer never
    depends on the order the entries happened to arrive in.
    """
    best: Optional[Any] = None
    best_key: Optional[Tuple[float, str]] = None
    for entry in entries:
        key = (_acquired_at(entry), _plan_id(entry))
        if (
            best_key is None
            or key[0] > best_key[0]
            or (key[0] == best_key[0] and key[1] < best_key[1])
        ):
            best, best_key = entry, key
    return best


def select_victim(snapshot: Snapshot, min_runtime_seconds: float) -> Optional[str]:
    """Return the plan that may be interrupted, or ``None`` if there is none.

    A plan is a candidate when it has been running for at least ``M``
    seconds — ``elapsed >= M``, and *not* ``elapsed > M``: the default
    600 seconds of protection is exactly that long, and a threshold
    that is off by one either voids the protection or stalls the
    scheduler for good.

    Among the candidates the **youngest** one is chosen — the plan that
    took its slot most recently — because interrupting the plan that is
    closest to finishing is what the operator would not forgive.

    ``None`` means there is nobody to interrupt: the running set is
    empty, or every plan in it is still inside its protected window.
    That is an answer, not an error, and the caller decides what to do
    with it.

    Whether an eviction is *permitted* is a question of its own and is
    not answered here: :func:`admission_verdict` is the gate, so that
    the two triggers that may interrupt a plan — an explicit start and
    the tick's convergence — share one candidate set and one rule.
    """
    threshold = _number(min_runtime_seconds, DEFAULT_MIN_RUNTIME_SECONDS)
    candidates = [
        entry for entry in _running_entries(snapshot) if _elapsed(entry) >= threshold
    ]
    victim = _youngest(candidates)
    return None if victim is None else _plan_id(victim)


# ---------------------------------------------------------------------------
# Convergence
# ---------------------------------------------------------------------------


def converge(snapshot: Snapshot, capacity: int) -> List[str]:
    """Return the plans to interrupt so the scheduler fits inside ``capacity``.

    When more plans hold a slot than there are slots, the scheduler
    converges: it interrupts the youngest eligible plans until it fits
    again, or until there is nobody left it may interrupt.  The plan ids
    come back in the order they are to be interrupted — the youngest
    first, then the next youngest among those still eligible.

    The loop is bounded by the number of plans holding a slot, because
    every round removes one of them from the candidate set.  A
    scheduler with nobody to interrupt therefore stops on its first
    round instead of spinning: convergence that cannot make progress
    reports what it could not do, and the tick decides what to do
    about it.
    """
    min_runtime = _min_runtime(snapshot)
    running = _running_entries(snapshot)
    used = _used(snapshot)
    target = int(_number(capacity, 0))

    candidates = list(running)
    victims: List[str] = []
    for _ in range(max(used, 0)):
        if used - len(victims) <= target:
            # It fits again — stop before touching anybody else.
            break
        victim = select_victim({"running": candidates}, min_runtime)
        if victim is None:
            # Nobody is old enough to be interrupted.
            break
        victims.append(victim)
        candidates = [entry for entry in candidates if _plan_id(entry) != victim]
    return victims