"""backend.plan_scheduler.snapshot — the scheduler's single read model.

One snapshot, read by everybody
-------------------------------
The operator panel, the LUI and the API all have to answer the same
three questions: *what is running*, *what is waiting*, and *why is it
waiting*.  If each of them answered from its own copy of the facts,
the panel and the LUI would disagree, and the operator would have no
way of telling which of the two is telling the truth.

So the facts are read once, in one place, and the answer is assembled
here.  This module **writes nothing**: it takes the state of a tick and
returns the document the panel renders and the LUI reports.  Every
decision it makes is a presentation decision — the order of a list, the
number on a position, the word on a reason — and none of them is a
scheduling decision.

The order is not this module's
------------------------------
The queue comes out sorted by :func:`plan_scheduler.queue.queue_key` —
the same function the admission check sorts by — and the module does
not own a second comparator.  A second comparator would be a second
answer to "who runs next", and the operator's only way of finding out
which answer the scheduler was using would be to watch a plan start in
the wrong order.

The reasons are not this module's either
----------------------------------------
A reason is a label the policy attaches to a decision, and the panel
maps it to a piece of text.  The vocabulary is closed — it is
:data:`plan_scheduler.policy.REASONS` — and a reason outside it is not
rendered as it stands: it falls back to a word the panel knows how to
draw.  The panel is never handed a label it cannot display, and it is
never handed one that means something else.

The shape is closed
-------------------
The document has exactly six keys: ``capacity``, ``queue_length``,
``over_capacity``, ``queue``, ``running`` and ``generated_at``.  A
field that appears without being declared is a field the panel cannot
render and the LUI cannot rely on, so adding one is a decision and not
an accident.  The two lists are always lists — an empty scheduler
produces two empty lists, never two ``null``s, because ``null`` would
be a second answer to "how many are waiting".

The queue row names its plan
----------------------------
A queued plan is a plan, and a row of numbers does not say which one it
is.  The queue row therefore carries the plan's own name and the
facts' answer to "may it start now" — the two the panel needs to show
the plan and to act on it.  A row that carries neither reads as the
empty name and as due: a plan nothing has been scheduled for is due,
because nothing holds it back.

The running row has two clocks
------------------------------
A running plan is measured twice, and the two numbers answer different
questions.  ``elapsed_sec`` is the **slot** clock — the time since the
scheduler took the plan's slot; it is measured against the tick, so it
starts again from zero whenever the scheduler restarts.
``running_sec`` is the **plan** clock — the time since the plan itself
started, measured from ``plan_execution.started_at``.

The eviction rule is applied to the slot clock and to nothing else: a
plan is protected for ``M`` seconds from the moment its slot was
taken, and a rule read off the plan clock would let a scheduler
restart make every long-running plan immediately evictable.  The panel
draws the plan clock — that is what 「已运行」 promises — and reads the
flag the backend decided.

The two flags are the same statement read twice
-----------------------------------------------
``over_capacity`` is true exactly when ``used`` is greater than the
total, and ``evictable`` is true exactly when the plan's **slot** time
has reached ``M``.  Both are computed from the numbers the panel shows
beside them, so a flag can never contradict the number it sits next
to.

The capacity says where it came from
-----------------------------------
The scheduler's capacity comes from one of two places: the
configuration file, or the operator's runtime override.  The snapshot
does not decide which one is in charge — the engine does, when it
reads the state — and it only says which one it was, so that the
panel can mark a number the operator set as an override and leave a
number that came from the file alone.

A state that names no source is read as ``config``: the configured
capacity is the ordinary case, and the override is the one that has to
be announced.  A source that *is* named is passed on as it is — the
snapshot does not rewrite a word it does not recognise, because the
layer that wrote it chose that word on purpose.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import DEFAULT_CAPACITY, DEFAULT_MIN_RUNTIME_SECONDS
from .policy import REASONS
from .queue import queue_key

__all__ = [
    "build",
    "CAPACITY_SOURCE_CONFIG",
    "CAPACITY_SOURCE_OVERRIDE",
    "DEFAULT_CAPACITY_SOURCE",
]


#: The reason a queued plan falls back to when the state carries a word
#: the policy does not know.  It is a word from the closed vocabulary,
#: and it says the one thing that is true of every queued plan: it is
#: waiting, and the thing it is waiting for is a slot.
DEFAULT_QUEUE_REASON = "waiting_for_slot"

#: The capacity came from the configuration file — the standing
#: setting, the one that is in charge unless an operator says
#: otherwise.
CAPACITY_SOURCE_CONFIG = "config"

#: The capacity came from the operator's runtime override — the number
#: that is in force right now.
CAPACITY_SOURCE_OVERRIDE = "override"

#: The source a state that names none is read as.  The configured
#: capacity is the ordinary case; the override is the one that has to
#: be announced.
DEFAULT_CAPACITY_SOURCE = CAPACITY_SOURCE_CONFIG


# ---------------------------------------------------------------------------
# Reading the state
# ---------------------------------------------------------------------------


def _field(row: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` out of ``row``, falling back to ``default``.

    A row is read the same way whatever shape it has: a mapping answers
    ``get``, anything else answers ``getattr``.  A value that is present
    but ``NULL`` reads as the default — for the snapshot's purposes an
    unset field and an absent one mean the same thing.
    """
    getter = getattr(row, "get", None)
    if callable(getter):
        value = getter(name, default)
    else:
        value = getattr(row, name, default)
    return default if value is None else value


def _number(value: Any, default: float) -> float:
    """Return ``value`` as a number, or ``default`` when it is not one.

    A number the snapshot cannot read is a number it cannot compare,
    and guessing would be worse than saying "not known": the plan is
    then treated as young, which protects it rather than evicting it.
    """
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _epoch(value: Any) -> Optional[float]:
    """Return ``value`` as epoch seconds, or ``None`` when it is not a time.

    Three shapes are understood — epoch seconds, a ``datetime`` and an
    ISO-8601 string — because the slot table stores its stamp as text
    while the state the tick builds carries a number.  ``None`` means
    "this is not a time I can read", and a moment that cannot be read
    cannot be subtracted from anything.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(
            tzinfo=timezone.utc
        )
        return moment.timestamp()
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _entries(state: Any, name: str) -> List[Any]:
    """Return the state's list under ``name``, or an empty list.

    A state that carries nothing is a normal thing to read — an
    idle scheduler has no queue and nothing running — so a missing or
    unreadable list is an empty list and not an error.
    """
    value = _field(state, name, [])
    try:
        return list(value)
    except TypeError:
        return []


def _now_iso() -> str:
    """Return the current moment the way the state database writes it.

    The format is the database's own: UTC, to the second, with the
    ``Z`` the timestamps are stored with.  The snapshot is read by a
    panel and by an LUI, and both have to be able to tell when it was
    made.
    """
    return (
        datetime.fromtimestamp(time.time(), tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


# ---------------------------------------------------------------------------
# The two lists
# ---------------------------------------------------------------------------


def _reason(row: Any) -> str:
    """Return the reason a queued plan is waiting, from the closed vocabulary.

    A reason the policy did not decide is not passed on: the panel maps
    a reason to a piece of text, and a word it has never heard would
    be rendered as a blank or as the word itself.  The fallback is a
    word from the vocabulary, so the panel is always able to draw what
    it is given.
    """
    value = _field(row, "reason", DEFAULT_QUEUE_REASON)
    if isinstance(value, str) and value in REASONS:
        return value
    return DEFAULT_QUEUE_REASON


def _promote_order(row: Any) -> Optional[int]:
    """Return the promotion number of a queued plan, or ``None``.

    ``None`` means the plan was never promoted, and the panel shows it
    as "not promoted" rather than as a number.
    """
    value = _field(row, "promote_order", None)
    if value is None:
        return None
    return int(_number(value, 0.0))


def _queue_view(state: Any) -> List[Dict[str, Any]]:
    """Return the queue as the panel reads it.

    The rows are sorted by the production comparator — the one the
    admission check uses — and the position of each row is its place in
    that order, counting from one.  The panel does not sort anything
    itself; what it renders is what the scheduler will actually do.

    A row names the plan it is about and says whether that plan may
    start now.  The two are passed on as they are: the name is the
    plan's own, and the flag is the facts' answer, read as due when the
    row does not carry one — a plan nothing has scheduled for is due,
    because nothing holds it back.
    """
    rows = sorted(_entries(state, "queue"), key=queue_key)

    return [
        {
            "position": position,
            "plan_id": str(_field(row, "plan_id", "")),
            "seq": int(_number(_field(row, "seq", 0), 0.0)),
            "priority": int(_number(_field(row, "priority", 0), 0.0)),
            "promote_order": _promote_order(row),
            "due": bool(_field(row, "due", True)),
            "reason": _reason(row),
        }
        for position, row in enumerate(rows, start=1)
    ]


def _started_text(value: Any) -> Optional[str]:
    """Return the plan's own start stamp as it was written, or ``None``.

    The stamp is passed on as it was written — surrounding whitespace
    aside, the snapshot does not rewrite what it cannot read, and a row
    that carries a stamp nobody can parse still shows the operator that
    stamp.  A missing stamp and a blank one both answer ``None`` —
    there is nothing to show.
    """
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _started_epoch(text: Optional[str]) -> Optional[float]:
    """Return the plan's own start time as epoch seconds, or ``None``.

    The stamp is a **naive local time** string: the execution route
    writes it with ``datetime.now().isoformat()``, and the very same
    string is what ``/api/system/active`` reports.  It is read here the
    way it was written — as local time — because a reader that assumed
    UTC would move every plan's start by exactly the offset it guessed,
    and on a machine that is not on UTC the number on the panel would
    be wrong by that much, always, and in the same direction.

    A stamp that carries its own offset is read with that offset: it
    says which zone it is in, so there is nothing to guess.  A stamp
    that cannot be read at all answers ``None`` — the caller then says
    "not known" rather than making a number up.
    """
    if not text:
        return None
    raw = text
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A naive stamp is local time — the machine's own zone, which
        # is the zone the writer used.  This is the same rule
        # ``shutdown_checkpoint`` applies to the same stamps.
        try:
            parsed = parsed.astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _running_view(state: Any, now: float, min_runtime: float) -> List[Dict[str, Any]]:
    """Return the running plans as the panel reads them.

    The elapsed time is measured against the moment the state carries —
    the tick's own clock — and not against the wall clock, so two reads
    of the same state say the same thing.  The flag beside it is the
    threshold applied to that very number, which is what makes the two
    impossible to contradict each other.

    A slot whose moment cannot be read is treated as young: it is
    protected rather than evicted, because a plan that is wrongly
    interrupted is worse than a plan that is wrongly protected.

    The row carries a second clock as well — the time since the plan
    itself started — and the two are not two readings of the same fact.
    ``elapsed_sec`` is the **slot** clock: it is measured from the
    moment the slot was acquired, so it starts again from zero
    whenever the scheduler restarts.  ``running_sec`` is the **plan**
    clock: it is measured from ``plan_execution.started_at``, so it
    keeps counting across a restart and says how long the plan has
    really been at it.  The eviction rule is applied to the slot clock
    and to nothing else — a plan is protected for ``M`` seconds from
    the moment its slot was taken, and a rule read off the plan clock
    would let a scheduler restart make every long-running plan
    immediately evictable.
    """
    view: List[Dict[str, Any]] = []

    for row in _entries(state, "running"):
        acquired = _epoch(_field(row, "acquired_at", None))
        elapsed = 0 if acquired is None else int(max(0.0, now - acquired))
        # The plan's own clock — measured from the start stamp, not
        # from the slot.  A stamp that cannot be read leaves the plan
        # clock unknown (``None``) rather than borrowing the slot
        # clock's number: the panel falls back to it on its own, and a
        # reader here that quietly filled the gap would be reporting a
        # plan's age in a unit it does not have.
        started_at = _started_text(_field(row, "started_at", None))
        started = _started_epoch(started_at)
        running = None if started is None else int(max(0.0, now - started))
        view.append(
            {
                "plan_id": str(_field(row, "plan_id", "")),
                "acquired_at": acquired,
                "elapsed_sec": elapsed,
                "running_sec": running,
                "started_at": started_at,
                # The eviction rule reads the **slot** clock, never the
                # plan clock: the protection is measured from the
                # moment the slot was acquired.
                "evictable": elapsed >= min_runtime,
                "workspace": _field(row, "workspace", None),
            }
        )

    return view


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------


def build(state: Any) -> Dict[str, Any]:
    """Assemble the scheduler's read model from the state of one tick.

    Args:
        state: the state of a tick — the capacity, where the capacity
            came from, how many plans hold a slot, the moment the tick
            read, the runtime a plan is protected for, the queued plans
            and the running ones.  A state that carries nothing at all
            is read as an idle scheduler rather than as an error.

    Returns:
        The document the panel renders and the LUI reports.  Its keys
        are exactly ``capacity``, ``queue_length``, ``over_capacity``,
        ``queue``, ``running`` and ``generated_at``.  The capacity
        block carries the number and its source — ``total``,
        ``used`` and ``source``.
    """
    running_rows = _entries(state, "running")

    total = int(
        _number(_field(state, "capacity", DEFAULT_CAPACITY), DEFAULT_CAPACITY)
    )
    used = int(
        _number(
            _field(state, "used", len(running_rows)), float(len(running_rows))
        )
    )
    # Where the number came from — the override or the configuration.
    # A state that names no source is read as the configured capacity,
    # and a source that is named is passed on as it is.
    source = _field(state, "capacity_source", DEFAULT_CAPACITY_SOURCE)
    now = _number(_field(state, "now", time.time()), time.time())
    min_runtime = _number(
        _field(state, "min_runtime_seconds", DEFAULT_MIN_RUNTIME_SECONDS),
        DEFAULT_MIN_RUNTIME_SECONDS,
    )

    queue = _queue_view(state)

    return {
        "capacity": {"total": total, "used": used, "source": source},
        "queue_length": len(queue),
        "over_capacity": used > total,
        "queue": queue,
        "running": _running_view(state, now, min_runtime),
        "generated_at": _now_iso(),
    }