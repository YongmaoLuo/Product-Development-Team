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
reason attached to it comes from :data:`REASONS`, a closed set of
thirteen words.  The panel renders them, the dry run records them and
the API publishes them; none of them composes a sentence of its own.  A
reason outside the set is a bug in the policy, not a new situation to be
described in a new word.

Eight of those words are the ones the vocabulary had before the
framework-fix lane; the other five are the lane's own — a plan that
runs first because it is a framework fix, a plan that was pushed aside
for one, a plan that is protected from being pushed aside, a plan
waiting for its repository's merge slot, and a plan whose wait has gone
on long enough to look like a deadlock.  The lane **adds** words; it
does not trade old ones away, and the ordering the lane gives its own
plans is a separate matter from the words themselves: it lives in
:data:`FRAMEWORK_FIX_PRIORITY`.

**The eviction candidate set is shared.**  The two triggers that may
interrupt a running plan — an operator asking for a plan to start now,
and the tick finding more plans running than there are slots — pick
their victim from the same candidates, by the same rule: a plan that
has been running for at least ``M`` seconds, and among those the plan
that stands **last in the line**.

The line is the queue's order.  The queue sorts plans by ascending
``priority``, so a smaller number stands *earlier* — the plan the
caller asked to be first — and the **larger** number stands later, and
is the one the scheduler can most afford to interrupt.  What the
priority leaves open is decided by the **runtime**: among plans of the
same priority the one that has run the *shortest* goes first, because
it has invested the least work and interrupting it loses the least.

Neither the creation time nor the slot timestamp has a say any more.
The moment a slot was taken is rewritten whenever the ledger is
reconciled, and a creation time says nothing about how much work a
plan has done; the two were the columns the old rule read, and the
old rule let a plan the caller deliberately pushed back be squeezed
out by a plan that stood later in the line.

**The rule can thrash.**  While the queue stays due and the scheduler
stays at its capacity, plan A gives way to plan B, B starts, and C —
queued behind B and standing later in the line — pushes B out again on
the next tick.  The order decides *who* goes first; it does not decide
*whether* the scheduler keeps evicting, and the protection window ``M``
is what damps that.

**A plan that is too young is not a candidate at all**, and an empty
candidate set is an answer rather than an error:
:func:`select_victim` returns ``None`` and :func:`converge` stops.

**A protected plan is not a candidate either.**  A framework-fix plan
is protected — two framework fixes interrupting each other would be a
livelock — and so is anything the snapshot marks as protected
(``protected: true`` / ``preemptable: false``).  The reason it carries
is ``protected_framework_fix``, a word of the closed vocabulary in
:data:`REASONS`.  :func:`is_protected` and :func:`protection_reason`
answer the question; :func:`select_victim` applies it.

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
             "priority": 0,             # the caller's number (optional)
             "plan_kind": "normal",     # "framework_fix" = protected
             "preemptable": True},      # False = never interrupted
        ],
        "allow_evict": True,            # may a running plan be interrupted?
        "min_runtime_seconds": 600,     # M — the runtime a plan is protected for
    }

**Where ``plan_kind`` and ``preemptable`` come from.**  They are the
protection facts, and they are produced by the **capacity ledger** — by
the candidate rows it hands over (``CapacityLedger.evictable``), which
read the routing view the ledger keeps.  The policy does not go and
look for them: it reads the two keys on the entry it is given, so a
caller that builds a running set out of something else has to carry the
two keys on it too.  An entry that carries neither cannot answer "is
this plan protected?" — and the answer it gives is "no".
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import DEFAULT_MIN_RUNTIME_SECONDS

__all__ = [
    "COUNTED_PHASES",
    "FRAMEWORK_FIX_KIND",
    "FRAMEWORK_FIX_PRIORITY",
    "ORDINARY_MIN_PRIORITY",
    "PROTECTED_FRAMEWORK_FIX_REASON",
    "REASONS",
    "Snapshot",
    "admission_verdict",
    "is_protected",
    "protection_reason",
    "select_victim",
    "converge",
]

#: The closed vocabulary of reasons.  Every decision the policy makes
#: carries one of these words, and the panel, the dry run and the API
#: all read the same thirteen.  It is a *set* on purpose: there is no
#: order to it, because a reason is a label and not a step.
#:
#: The first eight words listed are the ones the vocabulary had before
#: the framework-fix lane; the five listed after them are the lane's
#: own.  The lane adds words to the set and takes none away, so a reader
#: that knows the eight still recognises every one of them.
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
        # 2026-11-23 (framework-fix lane): the five words the lane
        # adds — a plan that runs first because it is a framework fix,
        # one that was pushed aside for one, one that is protected from
        # being pushed aside, one waiting for its repository's merge
        # slot, and one whose wait has gone on long enough to look like
        # a deadlock.
        "framework_fix_priority",
        "preempted_by_framework_fix",
        "protected_framework_fix",
        "waiting_merge_slot",
        "scheduler_deadlock_suspected",
    }
)

#: The priority the framework-fix lane puts a plan of its own kind on.
#:
#: Smaller runs first — the queue sorts plans by ascending priority —
#: so the lane's own plans have to sit on a level **below** the ordinary
#: ones to stand in front of them.  The value is the smallest there is
#: and it is *reserved*: it is the level a framework-fix plan occupies,
#: and :data:`ORDINARY_MIN_PRIORITY` is the level an ordinary plan
#: occupies.  Two levels, one order — a framework-fix plan always
#: stands ahead of an ordinary plan.
#:
#: This is the lane's own level, not the queue row's ``priority``
#: column: the column keeps deciding among the plans that share a
#: level, and the level itself is what puts the lane's plans in front.
FRAMEWORK_FIX_PRIORITY = 0

#: The priority an ordinary plan occupies in the framework-fix lane:
#: one level behind :data:`FRAMEWORK_FIX_PRIORITY`, and therefore
#: behind every framework-fix plan the lane puts in front of it.
ORDINARY_MIN_PRIORITY = FRAMEWORK_FIX_PRIORITY + 1

#: The word a plan's ``plan_kind`` carries when it is a repair of the
#: framework itself.  It is the same word
#: :data:`plan_scheduler.queue.FRAMEWORK_FIX_KIND` uses; the two packages
#: do not import each other (``queue`` imports *this* module), so the
#: word is repeated here — and the two MUST agree, or a framework-fix
#: plan would be protected under one name and not the other.
FRAMEWORK_FIX_KIND = "framework_fix"

#: The reason a running plan is protected from being interrupted: it is
#: a framework fix, and two framework fixes interrupting each other
#: would be a livelock.  It is one of the words in :data:`REASONS`, so
#: the panel, the dry run and the API all draw it.
PROTECTED_FRAMEWORK_FIX_REASON = "protected_framework_fix"

#: The phases in which a plan counts against the active quota.
#:
#: A plan counts while it **holds compute**: an execution subprocess
#: running (``executing``), or a verification round working on it (the
#: verification phases — ``user_stopping`` among them, because a plan
#: that is winding down still holds its worker, and a scheduler that
#: stopped counting it would hand its slot away too early).
#:
#: A plan that is *waiting* holds nothing and counts for nothing: it is
#: queued for its turn, parked for provider quota, or waiting for a
#: human to answer.  Neither does a plan that has finished.  The quota
#: is a budget of workers, so a plan that is not working must not spend
#: it.
COUNTED_PHASES = frozenset(
    {
        "executing",
        "verification_running",
        "verify_first_pass",
        "verify_recheck",
        "verification_repairing",
        "verification_rerunning",
        "user_stopping",
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

    A value the policy cannot read is a value it cannot compare, and
    guessing would be worse than saying "not known".  For a duration
    that means the plan is treated as young, which protects it rather
    than evicting it; for a priority it means the ordinary weight,
    which leaves the plan where every plan without a stated priority
    stands.
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


def _flag(value: Any) -> bool:
    """Read a boolean marker by its meaning.

    SQLite and JSON turn a boolean into ``0`` / ``1`` / ``"true"`` /
    ``"false"``, and all four spellings have been seen.  ``bool()`` on
    its own reads the string ``"false"`` as true.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in (
            "",
            "0",
            "false",
            "no",
            "none",
            "null",
            "off",
        )
    return bool(value)


def _elapsed(entry: Any) -> float:
    """Return how long the entry has been running, in seconds."""
    return _number(_value(entry, "elapsed", 0), 0.0)


def _priority(entry: Any) -> int:
    """Return the entry's priority, or ``0`` when the policy cannot read one.

    A priority is a whole number.  A value that is missing, ``NULL``,
    a boolean or not a number at all reads as ``0`` — the weight every
    plan carries when nothing says otherwise — and so does a number
    that is not whole.

    The reading never lifts a plan to the front of the order by
    accident: a priority the policy cannot read leaves the plan at the
    ordinary weight, and the runtime decides among the plans that
    share it.
    """
    number = _number(_value(entry, "priority", 0), 0.0)
    if not number.is_integer():
        # ``_number`` already reads a boolean and anything it cannot
        # parse as missing; both land on 0, and so does a number that
        # is not whole — a priority is a whole number.
        return 0
    return int(number)


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


def is_protected(entry: Any) -> bool:
    """Return whether a running plan is protected from being interrupted.

    A framework-fix plan is **always** protected: two framework fixes
    interrupting each other would be a livelock, and the lane's own
    admission rule (``preemptable`` is constantly false) says so
    structurally rather than by good luck.

    The snapshot may also say it directly — ``protected: true`` or
    ``preemptable: false`` — which is the same fact written another
    way; the scheduler honours either spelling.
    """
    kind = str(_value(entry, "plan_kind", ""))
    if kind == FRAMEWORK_FIX_KIND:
        return True
    if _flag(_value(entry, "protected", False)):
        return True
    preemptable = _value(entry, "preemptable", None)
    if preemptable is not None and not _flag(preemptable):
        return True
    return False


def protection_reason(entry: Any) -> Optional[str]:
    """Return why a running plan may not be interrupted, or ``None``.

    The reason is one of the words in :data:`REASONS` —
    ``protected_framework_fix`` — so the panel, the dry run and the API
    all draw it from the same closed vocabulary.
    """
    if not is_protected(entry):
        return None
    return PROTECTED_FRAMEWORK_FIX_REASON


def select_victim(snapshot: Snapshot, min_runtime_seconds: float) -> Optional[str]:
    """Return the plan that may be interrupted, or ``None`` if there is none.

    A plan is a candidate when it has been running for at least ``M``
    seconds — ``elapsed >= M``, and *not* ``elapsed > M``: the
    protection window is exactly that long, and a threshold that is off
    by one either voids the protection or stalls the scheduler for good.

    A plan that is **protected** is not a candidate at all, however
    long it has been running: a framework-fix plan is protected
    (:func:`is_protected`), because two framework fixes interrupting
    each other would be a livelock.  The reason it carries is
    ``protected_framework_fix`` — a word of the closed vocabulary in
    :data:`REASONS`.

    Among the candidates the order is **two stages**:

    * **The priority first.**  The queue sorts plans by ascending
      ``priority``, so the plan that stands *last* in the line is the
      one with the **larger** number — and it is the one the scheduler
      can most afford to interrupt.  A plan the caller deliberately
      pushed back (``priority = -10``) stands first and keeps its slot,
      whatever it has been running for.
    * **The runtime second.**  Among plans of the same priority the one
      that has run the **shortest** goes first: it has invested the
      least work, and interrupting it loses the least.

    A tie is broken by ``plan_id`` in dictionary order, so the answer
    never depends on the order the entries happened to arrive in.

    **The rule can thrash.**  While the queue stays due and the
    scheduler stays at its capacity, plan A gives way to plan B, B
    starts, and C — queued behind B and standing later in the line —
    pushes B out again on the next tick.  The order decides *who* goes
    first; it does not decide *whether* the scheduler keeps evicting,
    and the protection window ``M`` is what damps that.

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
        entry
        for entry in _running_entries(snapshot)
        if _elapsed(entry) >= threshold and not is_protected(entry)
    ]
    if not candidates:
        return None
    # The sort key, in one place: the priority descending (the last in
    # the line first), the runtime ascending (the shortest run first)
    # and the name ascending (a total order, so the answer does not
    # depend on the order the entries happened to arrive in).
    victim = min(
        candidates,
        key=lambda entry: (-_priority(entry), _elapsed(entry), _plan_id(entry)),
    )
    return _plan_id(victim)


# ---------------------------------------------------------------------------
# Convergence
# ---------------------------------------------------------------------------


def converge(snapshot: Snapshot, capacity: int) -> List[str]:
    """Return the plans to interrupt so the scheduler fits inside ``capacity``.

    When more plans hold a slot than there are slots, the scheduler
    converges: it interrupts the eligible plans until it fits again, or
    until there is nobody left it may interrupt.  The plan ids come
    back in the order they are to be interrupted — the plan that stands
    last in the line first, then the next one down among those still
    eligible.

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