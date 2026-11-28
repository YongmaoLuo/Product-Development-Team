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

The phase is passed on as it is
-------------------------------
A phase says what a plan is *doing* — executing, repairing,
verifying — and it is the routing layer's own word: the plan's
``plan_routing.current_phase``, read by the facts and carried on the
row the engine writes into the state.  The snapshot does not know the
phase vocabulary and does not map the phase to a label: a label is
worked out further up, and a second mapping here would be a second
answer to "what is this plan doing".

A row that carries no phase says ``None``.  A state written before the
phase existed is a normal thing to read, and answering "I don't know"
is the right answer to it — raising here would take the whole read
model down over a field that is allowed to be absent.

The shape is closed
-------------------
The document has exactly ten keys: ``capacity``, ``queue_length``,
``over_capacity``, ``queue``, ``running``, ``waiting``, ``parked``,
``framework_fixes``, ``stopping`` and ``generated_at``.  A field that
appears without being declared is a field the panel cannot render and
the LUI cannot rely on, so adding one is a decision and not an
accident.  The six lists are always lists — an empty scheduler
produces six empty lists, never six ``null``s, because ``null``
would be a second answer to "how many are waiting".

The seventh key was added on purpose
------------------------------------
The document had six keys until the panel could not show what the
operator was waiting for: the queue's reason column said 「等待人工回
答」 and stopped there — no question, no time left, no way of telling
one waiting plan from another.  The waiting list is that answer
spelled out, and the key set was widened **explicitly** rather than
loosened into a subset: a subset assertion would let the next key in
without anybody deciding to add it.

The waiting row is a closed shape too
-------------------------------------
A waiting row carries exactly eleven keys — the plan, the item, the
kind of wait, the question, the deadline, the time left, whether the
wait can still be pushed out, how many times it has been, the source
the wait was asked under and the pull request it is about.  A twelfth
key would be a field the panel cannot draw, so the row is pinned the
same way the document is.

The last three keys are the **review fields**: the source the
finishing stage asked the question under, the number of the pull
request the question is about and the link to it.  They are passed on
as the record wrote them, and a record that carries none of them
answers the empty source and an unknown pull request — a wait written
before the fields existed is still a row the reader can read.

The time left is counted, not guessed
------------------------------------
``remaining_seconds`` is measured against the **tick's own clock** —
the moment the state carries — and not against the wall clock, so two
reads of the same state say the same thing and the countdown on the
panel does not jump between two reads nobody asked for.  A deadline
that has passed answers zero: the panel counts down, and a countdown
below zero would be showing a time that does not exist.

The terminal summary comes from the report
------------------------------------------
The finishing stage's report — ``postprocess_report.json`` — is the
terminal truth: the panel's terminal area, the failure notification and
the resume entry point all read it.  The summary is that same truth
with three of its fields, so the panel and the report cannot tell the
operator two different stories.

It is a **read**, like the rest of this module: it starts no
subprocess, opens no database and writes nothing.  It is also not a
key of the document above — the summary belongs to the plan entry the
terminal area shows, not to the scheduler's own facts — so it is built
by its own function, :func:`postprocess_summary`, and attached to a
plan entry by :func:`with_postprocess`.

The eighth key was added; the others did not move
-------------------------------------------------
The document used to carry six keys, and the six were a closed
contract pinned by three tests.  ``parked`` was added beside them and
not instead of one of them: the names, the types and the meanings of
the other keys are unchanged, and the rows of the queue and of the
running line keep their key sets.  A reader written against the six
keys is still reading a document it was written for — it finds more
keys, which is what a backward-compatible addition looks like.

The ninth key is the framework-fix lane's own column
---------------------------------------------------
The document carries a ninth key: ``framework_fixes``.  It is the
framework-fix lane's own plans — the ones the routing layer says are
``plan_kind="framework_fix"`` — and the four ordinary lists do **not**
name them: a repair plan is in the framework-fix column or in the
ordinary one, never in both.  A panel that showed the same plan twice
would show the operator two different things.

The partition is read from the lane's own tables — the routing row,
the registry, the attach table and the merge lease — and every entry
carries the ``display_state`` the framework-fix lane's own read model
projects (:func:`framework_fix.read_model.project_display_state`), so
the panel, the notification and this document cannot tell the operator
three different stories about one plan.

The key is always there: no repair plan is an **empty list**, not a
missing key.  A caller that walks the column must not have to ask
whether the column exists first.

The parked row is not a queued row
----------------------------------
A parked plan is neither running nor waiting: it holds no slot and it
is in no line, so it is in neither of the two lists above.  Its row
therefore has its own shape — the plan, the side that stopped, the
round, the reason and the phase — and the reason it carries is the
quota layer's own word rather than the queue policy's, because a
parked plan stopped for want of provider capacity and not for want of
a slot.

The tenth key is ``stopping``
------------------------------
A plan that is asked to pause leaves the running line **before** it
comes back: the stop is a request the worker has not answered yet, and
in that gap the plan is in neither the running line nor the queue.  The
``stopping`` list is that gap made visible — one row per command the
scheduler has **accepted and not answered yet**.

The rows come from the **command log**, which the engine reads and
hands over in the state's ``stopping`` key: a command is written the
moment it is taken, with the outcome it has *so far* (``accepted``),
and it is turned into ``applied`` or ``failed`` only once the worker
has answered.  A command that is still ``accepted`` is therefore a
command that is still in flight.

The age is measured against the **tick's own clock**, like the waiting
countdown, so two reads of the same state say the same thing.  A
timestamp that cannot be read answers ``None`` instead of dropping the
row: the operator has to see that the plan is stopping even when the
moment it was taken is unreadable.


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

The running row names the plan's weight
---------------------------------------
A running plan is not merely present: it is holding a slot, and when
a slot has to be given up the running plans are compared by the
**priority** the caller gave them — the one that stands last in the
line goes first.  The row therefore carries that number, read off the
state as it is.  The queued rows have always carried it, so a panel
that could show the order of the plans *waiting* but not of the plans
*running* was answering half the question.

The number is read, not invented: a row that carries no usable
priority — a slot written before the column existed, a ``NULL``, a
value that is not a number — reads as ``0``, the ordinary weight every
plan without a stated priority stands at.

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

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from postprocess.report import read_report

from quota_resume import KIND_EXECUTION, QUOTA_PARK_REASON

from config_paths import resolve_plans_dir, resolve_state_db_path

from state_machine.db.connection import open as open_state_db

try:  # pragma: no cover - 两条路径等价，取先成功的那条
    from framework_fix.read_model import project_display_state
except ImportError:  # pragma: no cover
    from backend.framework_fix.read_model import project_display_state  # type: ignore

from .config import DEFAULT_CAPACITY, DEFAULT_MIN_RUNTIME_SECONDS
from .policy import REASONS
from .queue import FRAMEWORK_FIX_KIND, queue_key

__all__ = [
    "build",
    "framework_fix_facts",
    "framework_fix_view",
    "postprocess_summary",
    "with_postprocess",
    "CAPACITY_SOURCE_CONFIG",
    "CAPACITY_SOURCE_OVERRIDE",
    "DEFAULT_CAPACITY_SOURCE",
    "FRAMEWORK_FIXES_KEY",
    "KIND_ALL",
    "KIND_FRAMEWORK_FIX",
    "KIND_PLANS",
    "POSTPROCESS_KEYS",
    "STOPPING_KEYS",
    "WAITING_KEYS",
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

#: The waiting row's eleven keys — the shape the panel draws.  A row
#: that carried a twelfth key would be a field the panel cannot render,
#: so the shape is closed here as firmly as the document's own.  The
#: last three are the review fields: the source the wait was asked
#: under and the pull request it is about.
WAITING_KEYS = frozenset(
    {
        "plan_id",
        "item_id",
        "wait_type",
        "question",
        "deadline",
        "remaining_seconds",
        "extendable",
        "extensions_count",
        "source",
        "pr_number",
        "pr_url",
    }
)

#: The stopping row's four keys — the shape the panel draws.  The row
#: names the plan and the action, says when the command was taken and
#: how long ago that was; a fifth key would be a field the panel cannot
#: render.
STOPPING_KEYS = frozenset(
    {
        "plan_id",
        "action",
        "since",
        "age_seconds",
    }
)

#: The terminal summary's three keys — the report's own fields, under
#: the names the panel reads them by.
POSTPROCESS_KEYS = frozenset({"spec_sha256", "terminal_status", "item_verdicts"})

#: The key the framework-fix lane's plans live under.  It is a key of the
#: document beside the four ordinary lists: the panel renders it as its
#: own column, and the ordinary lists do not name the lane's plans.
FRAMEWORK_FIXES_KEY = "framework_fixes"

#: The kinds of plan the API can be asked for.  The vocabulary is
#: closed: a word outside it cannot be answered, so it is a 422 rather
#: than a quiet ``all``.
KIND_ALL = "all"
KIND_PLANS = "plans"
KIND_FRAMEWORK_FIX = "framework_fix"

#: The attach statuses the partition counts.  A **blocked** attachment
#: is a plan whose work waits for this defect; a **registered** one is
#: a plan that will be re-run once the fix lands.  The two numbers are
#: the panel's "how much is waiting" and "how much is coming back".
_ATTACH_COUNTED_STATUSES = ("blocked", "registered")

#: The wait statuses that are **settled**.  A wait that is not pending
#: any more cannot be extended: its deadline is a deadline nobody is
#: watching any more.
SETTLED_WAIT_STATUSES = frozenset({"answered", "terminated", "failed"})

#: The fields that mark a row as a wait record rather than a queued
#: plan.  The engine's own ``waiting`` key means "plans waiting for a
#: slot", and those rows must not be shown as waits.
_WAIT_MARKERS = ("item_id", "wait_type", "question", "deadline", "extensions")


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


def _phase(row: Any) -> Optional[str]:
    """Return the phase the plan is in, or ``None`` when it has none.

    The phase is the routing layer's word and it is passed on as it
    is — the snapshot does not know the phase vocabulary, and the label
    a human reads is worked out further up.

    A row that carries no phase answers ``None``: a state written
    before the phase existed is a normal thing to read, and "I don't
    know" is the right answer to it.
    """
    return _field(row, "phase", None)


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

    The row also carries the phase — what the plan is doing — as it
    is.  A row without one says ``None`` rather than dropping the
    field, because the field is part of the row's shape and a row
    that sometimes has it and sometimes does not is a shape the panel
    cannot rely on.
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
            "phase": _phase(row),
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

    The row carries the plan's **priority** as well: the weight the
    caller gave it, which is what the eviction rule compares the
    running plans by.  It is read as it is — the sign is kept, and a
    row that carries no usable number reads as ``0``.

    The row carries the phase as well — what the plan is doing, in the
    routing layer's own words — and a row without one says ``None``.
    The phase is what tells the panel that "running" is not one thing
    but several: a plan executing and a plan repairing both hold a
    slot, and the operator has to be able to tell them apart.

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
                # The weight the caller gave the plan — the number the
                # eviction rule compares the running plans by.  Read as
                # it is; a row without a usable one reads as ``0``,
                # the ordinary weight.
                "priority": int(_number(_field(row, "priority", 0), 0.0)),
                # What the plan is doing, in the routing layer's own
                # words — a row without one says ``None``.
                "phase": _phase(row),
            }
        )

    return view


def _parked_view(state: Any) -> List[Dict[str, Any]]:
    """Return the parked plans as the panel reads them.

    A parked plan is neither running nor waiting: it holds no slot, so
    the running line does not name it, and it is in no line, so the
    queue does not name it either.  It stopped for want of provider
    capacity, and it is the one plan an operator has to be able to see
    — it will start again by itself once the window opens.

    The row says which plan it is (``plan_id``), which side of it
    stopped (``kind`` — execution or verification), which verification
    round it stopped in (``round``), why it stopped (``reason``) and
    what it is doing (``phase``).

    The **reason** is passed on as it is, and it is deliberately not
    read through the queue's own fallback: ``waiting_for_quota`` is
    not in the queue policy's closed vocabulary, and mapping it there
    would tell the operator that a plan parked for quota is waiting for
    a *slot* — a different fact, and a wrong one.  The phase travels
    as it is too, for the same reason the other rows' phases do: a
    label is the routing layer's business.

    A field the row does not carry answers its default.  The defaults
    come from the quota layer, which owns that vocabulary: a park has
    exactly one reason — the plan is waiting for quota — so a row that
    names none is read as that one, and the word is the quota layer's
    own constant rather than a second spelling of it here.  A row
    that names no kind is read as the execution side, which is the side
    the facts report first when a plan is parked on both.  The round
    is zero and the phase is ``None``.

    The order is the one the state carries: the facts hand the parked
    plans over in plan-id order, and the snapshot owns no second
    ordering of its own for plans that are not competing for a slot.
    """
    return [
        {
            "plan_id": str(_field(row, "plan_id", "")),
            "kind": str(_field(row, "kind", KIND_EXECUTION)),
            "round": int(_number(_field(row, "round", 0), 0.0)),
            "reason": str(_field(row, "reason", QUOTA_PARK_REASON)),
            "phase": _phase(row),
        }
        for row in _entries(state, "parked")
    ]


# ---------------------------------------------------------------------------
# The waiting list
# ---------------------------------------------------------------------------


def _is_wait_record(row: Any) -> bool:
    """Whether a row is a wait record rather than a queued plan.

    The engine's own ``waiting`` key means "the plans that are waiting
    for a slot", and those rows carry a plan, a priority and a
    sequence — none of the fields a wait carries.  A row that names an
    item, a kind of wait, a question, a deadline or a set of
    extensions is a wait, whatever list it was found in.
    """
    return any(_field(row, marker, None) is not None for marker in _WAIT_MARKERS)


def _wait_rows(state: Any) -> List[Any]:
    """The finishing stage's waits, as the state carries them.

    The waits are read from ``waits`` — the key that says what they
    are.  A state that lists them under ``waiting`` is read as well,
    but only the rows that are wait records: the engine's ``waiting``
    key means something else entirely, and showing its queued plans in
    the panel's waiting block would be a second answer to "what is
    waiting".
    """
    rows = list(_entries(state, "waits"))

    for row in _entries(state, "waiting"):
        if _is_wait_record(row) and row not in rows:
            rows.append(row)

    return rows


def _deadline_text(value: Any) -> Optional[str]:
    """The wait's deadline as the text the panel shows.

    The stamp is passed on as it was written — the registry writes it
    in ISO-8601 — and a stamp that is not text at all is read as
    "unknown" rather than guessed at.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _remaining_seconds(deadline: Optional[str], now: float) -> int:
    """How much time is left on a wait, in whole seconds.

    The countdown is measured against the tick's own clock, so two
    reads of the same state say the same thing.  A deadline that has
    passed answers **zero** and never a negative number: the panel
    counts down, and a countdown below zero would be showing a time
    that does not exist.

    A deadline that cannot be read answers zero as well.  The wait is
    then not known to have time left, and the number the panel shows
    is the one the wait's own record can support.
    """
    moment = _epoch(deadline)
    if moment is None:
        return 0
    return int(max(0.0, moment - now))


def _extendable(row: Any) -> bool:
    """Whether the wait's deadline can still be pushed out.

    A wait is extendable exactly while it is **pending** — the same
    rule the extend action itself applies.  A record that carries no
    status is read as a live wait: the registry always writes one, and
    a record without one is not a settled wait.
    """
    status = _field(row, "status", None)
    if not isinstance(status, str) or not status.strip():
        return True
    return status.strip().lower() not in SETTLED_WAIT_STATUSES


def _extensions_count(row: Any) -> int:
    """How many times the wait's deadline has been pushed out."""
    value = _field(row, "extensions", [])
    try:
        return len(list(value))
    except TypeError:
        return 0


def _waiting_view(state: Any, now: float) -> List[Dict[str, Any]]:
    """The finishing stage's waits, as the panel reads them.

    One row per wait, in the order the state listed them.  The row
    names the plan and the item, passes on the kind of wait and the
    question as they were written, and answers the two things the
    operator asks of a countdown: how long is left, and can it still be
    pushed out.

    The kind of wait is passed on as the record wrote it.  The registry
    refuses to create a wait with a kind outside its two-word
    vocabulary, so the record the panel is handed is already a word it
    knows — and a hand-made record that names something else is shown
    as it is, because the read model does not rewrite a word it cannot
    vouch for.

    The last three keys are the **review fields** — the source the
    wait was asked under and the pull request it is about.  They are
    passed on as the record wrote them, and a record that carries none
    of them answers the empty source and an unknown pull request: a
    wait written before the fields existed is still a row the reader
    can read.
    """
    view: List[Dict[str, Any]] = []

    for row in _wait_rows(state):
        deadline = _deadline_text(_field(row, "deadline", None))
        view.append(
            {
                "plan_id": str(_field(row, "plan_id", "")),
                "item_id": str(_field(row, "item_id", "")),
                "wait_type": str(_field(row, "wait_type", "")),
                "question": str(_field(row, "question", "")),
                "deadline": deadline,
                "remaining_seconds": _remaining_seconds(deadline, now),
                "extendable": _extendable(row),
                "extensions_count": _extensions_count(row),
                "source": str(_field(row, "source", "")),
                "pr_number": _field(row, "pr_number", None),
                "pr_url": _field(row, "pr_url", None),
            }
        )

    return view


# ---------------------------------------------------------------------------
# The framework-fix partition
# ---------------------------------------------------------------------------


def _query(conn: Any, sql: str, parameters: Sequence[Any] = ()) -> List[Any]:
    """Run one read and return its rows.

    A query that cannot run — a database that has not been migrated, a
    table that is not there — answers **no rows** rather than raising.
    The partition is then empty, which is the safe direction: a read
    model that cannot see the lane's plans says so, and it does not
    take the whole snapshot down over it.
    """
    try:
        return list(conn.execute(sql, parameters))
    except Exception:  # noqa: BLE001 — a read must not take the snapshot down
        return []


def _json_object(value: Any) -> Dict[str, Any]:
    """Read a JSON column as an object.

    Anything that is not an object — a column that was never written, a
    column that holds a list, a column that holds broken JSON — is an
    empty object.  A field that cannot be read is "not known", and
    guessing a shape for it would be a second answer to whatever it
    was supposed to say.
    """
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _in_clause(values: Sequence[str]) -> str:
    """The ``IN`` clause for a list of values, as SQL text."""
    return ", ".join("?" for _ in values)


def framework_fix_facts() -> Dict[str, Any]:
    """Read the framework-fix lane's own facts from the state database.

    The facts are the lane's own tables: the routing layer's answer to
    "which plans are the lane's own" (``plan_routing``), the registry's
    answer to "what defect does each one repairs", the attach table's
    counts and the merge lease's PR link.

    A database that cannot be read answers an empty set of facts: the
    partition is then empty and the ordinary lists are not filtered.  A
    read model that cannot see the lane's plans must not pretend it has
    seen them, and it must not take the whole snapshot down over it
    either.

    Returns:
        A mapping with five keys:

        * ``plans`` — the routing rows of the lane's plans, each with
          its ``plan_id``, its ``current_phase`` and its
          ``framework_fix`` block;
        * ``registry`` — the registry row of each plan, by plan id;
        * ``attaches`` — the attach counts of each fingerprint, by
          status;
        * ``leases`` — the merge lease of each repository, by
          repository;
        * ``hits`` — the first hit of each fingerprint, by fingerprint.
    """
    facts: Dict[str, Any] = {
        "plans": [],
        "registry": {},
        "attaches": {},
        "leases": {},
        "hits": {},
    }

    try:
        path = resolve_state_db_path()
        if not path.exists():
            return facts
        conn = open_state_db(path)
    except Exception:  # noqa: BLE001 — a read must not take the snapshot down
        return facts

    try:
        conn.row_factory = sqlite3.Row

        plans = _query(
            conn,
            "SELECT plan_id, current_phase, framework_fix FROM plan_routing "
            "WHERE plan_kind = ?",
            (FRAMEWORK_FIX_KIND,),
        )
        facts["plans"] = [
            {
                "plan_id": str(row["plan_id"] or ""),
                "current_phase": row["current_phase"],
                "block": _json_object(row["framework_fix"]),
            }
            for row in plans
            if str(row["plan_id"] or "")
        ]

        plan_ids = [str(plan["plan_id"]) for plan in facts["plans"]]
        if not plan_ids:
            return facts

        registry: Dict[str, Any] = {}
        for row in _query(
            conn,
            "SELECT fingerprint, fix_plan_id, repo, status, key_json, "
            "first_seen_at, created_at, merged_at, merge_commit "
            "FROM framework_fix_registry WHERE fix_plan_id IN ("
            + _in_clause(plan_ids)
            + ")",
            plan_ids,
        ):
            registry.setdefault(str(row["fix_plan_id"] or ""), dict(row))
        facts["registry"] = registry

        fingerprints = sorted(
            {
                str(row.get("fingerprint") or "")
                for row in registry.values()
                if str(row.get("fingerprint") or "")
            }
        )
        repos = sorted(
            {
                str(row.get("repo") or "")
                for row in registry.values()
                if str(row.get("repo") or "")
            }
        )

        if fingerprints:
            marks = _in_clause(fingerprints)
            counts: Dict[str, Dict[str, int]] = {}
            for row in _query(
                conn,
                "SELECT fingerprint, status FROM framework_fix_attach "
                "WHERE fingerprint IN (" + marks + ")",
                fingerprints,
            ):
                fingerprint = str(row["fingerprint"] or "")
                status = str(row["status"] or "")
                if status not in _ATTACH_COUNTED_STATUSES:
                    continue
                counted = counts.setdefault(fingerprint, {})
                counted[status] = counted.get(status, 0) + 1
            facts["attaches"] = counts

            hits: Dict[str, Any] = {}
            for row in _query(
                conn,
                "SELECT fingerprint, plan_id, round FROM framework_fix_hit "
                "WHERE fingerprint IN (" + marks + ") ORDER BY created_at, hit_id",
                fingerprints,
            ):
                hits.setdefault(
                    str(row["fingerprint"] or ""),
                    {
                        "plan_id": str(row["plan_id"] or ""),
                        "round": int(row["round"] or 0),
                    },
                )
            facts["hits"] = hits

        if repos:
            for row in _query(
                conn,
                "SELECT repo, fix_plan_id, pr_url FROM framework_fix_merge_lease "
                "WHERE repo IN (" + _in_clause(repos) + ")",
                repos,
            ):
                facts["leases"].setdefault(str(row["repo"] or ""), dict(row))
    finally:
        conn.close()

    return facts


def _plan_title(plan_id: str) -> str:
    """The plan's own title — the one line it says what it is.

    The repair plan writes its requirement into ``tasks.json`` when it
    is created.  A plan whose file cannot be read has no title: an
    empty string is a title the panel can show as "no title", and a
    made-up one would be worse.
    """
    try:
        path = resolve_plans_dir() / plan_id / "tasks.json"
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a missing title is not an error
        return ""
    if not isinstance(document, dict):
        return ""
    requirement = document.get("requirement")
    return str(requirement).strip() if requirement else ""


def _signature_summary(key: Mapping[str, Any]) -> str:
    """The signature of a defect, in the words the panel shows.

    The signature is the fingerprint's key: the error class, the first
    error line and the framework files the fix will touch.  The summary
    is the first two of them and the names of the files — one line,
    because a column that wraps is a column nobody reads.
    """
    klass = str(key.get("error_class") or "").strip()
    line = str(key.get("first_error_line") or "").strip()
    head = f"{klass}: {line}" if klass and line else (klass or line)

    raw = key.get("framework_files")
    if isinstance(raw, str):
        raw = [raw]
    files = [str(item).strip() for item in raw or [] if str(item).strip()]
    if not files:
        return head

    shown = ", ".join(files[:3])
    if len(files) > 3:
        shown = f"{shown} +{len(files) - 3}"
    return f"{head} [{shown}]"


def _partition_entry(
    plan: Mapping[str, Any], facts: Mapping[str, Any]
) -> Dict[str, Any]:
    """One entry of the framework-fix partition.

    The fields are the ones the panel and the notification both show
    (架构决策点 6): the plan and its title, the kind, where the defect
    came from, the signature in one line, the state it displays, the
    two attach counts and the PR link.

    The state is the framework-fix lane's own projection — the same
    function the notification reads — and it falls back to the plan's
    own phase when there is no special state to show.  A repair plan
    that is simply running has a state too: its phase.
    """
    plan_id = str(plan.get("plan_id") or "")
    block = plan.get("block")
    block = dict(block) if isinstance(block, Mapping) else {}

    registry = facts.get("registry") or {}
    registry_row = registry.get(plan_id) or {}
    fingerprint = str(
        registry_row.get("fingerprint") or block.get("fingerprint") or ""
    )
    repo = str(registry_row.get("repo") or block.get("repo") or "")

    # The counts are the attach table's own — the live ones, and only
    # the live ones.  The generator's snapshot of them is the count of
    # the moment the plan was created: it is not a fallback, because it
    # stays where it is after an attachment is exhausted, and a read
    # model that answered it would say a number the table does not
    # support.
    counts = (facts.get("attaches") or {}).get(fingerprint) or {}
    blocked = int(counts.get("blocked", 0) or 0)
    registered = int(counts.get("registered", 0) or 0)

    # Where the defect came from: the plan that hit it and the round it
    # hit it in.  The hit table knows the round; the routing block
    # knows the plan.
    hit = (facts.get("hits") or {}).get(fingerprint) or {}
    source_plan_id = str(
        block.get("source_plan_id") or hit.get("plan_id") or ""
    )
    source_round = int(hit.get("round") or block.get("source_round") or 0)

    # The PR link is this plan's own: the merge slot of a repository
    # belongs to the plan that holds it, and a plan that waits for the
    # slot has no PR yet.
    lease = (facts.get("leases") or {}).get(repo) or {}
    pr_url = None
    if str(lease.get("fix_plan_id") or "") == plan_id:
        pr_url = lease.get("pr_url") or None

    display_state = project_display_state(
        {
            "current_phase": plan.get("current_phase"),
            "framework_fix": block,
        },
        registry_row or None,
    )
    if not display_state:
        display_state = str(plan.get("current_phase") or "") or None

    return {
        "plan_id": plan_id,
        "title": _plan_title(plan_id),
        "kind": FRAMEWORK_FIX_KIND,
        "source": {"plan_id": source_plan_id, "round": source_round},
        "signature_summary": _signature_summary(
            _json_object(registry_row.get("key_json"))
        ),
        "display_state": display_state,
        "attach_blocked_count": blocked,
        "attach_registered_count": registered,
        "pr_url": pr_url,
        "created_at": str(
            block.get("created_at") or registry_row.get("created_at") or ""
        ),
    }


def framework_fix_view(state: Any) -> List[Dict[str, Any]]:
    """The framework-fix partition — the lane's own plans, as the panel reads them.

    Args:
        state: the state of one tick.  A state that carries the lane's
            facts under ``framework_fix_facts`` is read from it — a
            caller that has already read them does not read them
            twice; otherwise the facts are read from the state
            database.

    Returns:
        One entry per framework-fix plan, in plan-id order.  No repair
        plan is an **empty list**, not a missing key: a caller that
        walks the column must not have to ask whether the column is
        there first.
    """
    facts = _field(state, "framework_fix_facts", None)
    if not isinstance(facts, Mapping):
        facts = framework_fix_facts()

    entries = [
        _partition_entry(plan, facts)
        for plan in (facts.get("plans") or [])
        if isinstance(plan, Mapping)
    ]
    return sorted(entries, key=lambda entry: str(entry.get("plan_id") or ""))


# The stopping list
# ---------------------------------------------------------------------------


def _since_text(value: Any) -> Optional[str]:
    """The moment the command was taken, as the text the panel shows.

    The stamp is passed on as it was written — the command log writes it
    in ISO-8601 — and a stamp that is not text at all is read as
    "unknown" rather than guessed at.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _stopping_view(rows: Any, now: float) -> List[Dict[str, Any]]:
    """The commands that were accepted and have not been answered yet.

    One row per open command, in the order the rows were given.  The
    row names the plan and the action, says when the command was taken
    and how long ago that was.

    The age is measured against the **tick's own clock** — the moment
    the state carries — so two reads of the same state say the same
    thing, exactly like the waiting countdown.

    A timestamp that cannot be read costs the row its age and nothing
    else: the answer is ``None`` and the row is still there.  Dropping
    it would hide the very thing the row exists to show — that the
    plan is stopping.
    """
    view: List[Dict[str, Any]] = []

    for row in rows or []:
        taken = _field(row, "created_at", None)
        moment = _epoch(taken)
        view.append(
            {
                "plan_id": str(_field(row, "plan_id", "")),
                "action": str(_field(row, "action", "")),
                "since": _since_text(taken),
                "age_seconds": None if moment is None else float(now - moment),
            }
        )

    return view


def _open_command_rows(state: Any) -> List[Any]:
    """The open commands the state carries.

    They are read from the state's ``stopping`` key — the name the
    document gives them — and from ``open_commands``, the name the
    store's own reader gives them.  A state that carries neither is an
    empty list: nothing has been accepted and left unanswered, which is
    the ordinary case.
    """
    for name in ("stopping", "open_commands"):
        rows = _entries(state, name)
        if rows:
            return rows
    return []


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
        ``queue``, ``running``, ``waiting``, ``parked``,
        ``framework_fixes``, ``stopping`` and ``generated_at``.  The
        capacity block carries the number and its source — ``total``,
        ``used`` and ``source`` — the waiting list carries one row per
        wait, with the question and the time left on it, the parked list
        carries the plans stopped for want of provider capacity, and the
        stopping list carries the commands that were accepted and have
        not been answered yet.

        The framework-fix lane's own plans are in their own column and
        in **no** other one: a repair plan is either in the
        framework-fix partition or in the ordinary lists, never in
        both.
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

    # The framework-fix lane's own column — read first, because the
    # ordinary lists are filtered by it.
    partition = framework_fix_view(state)
    fix_ids = {str(entry.get("plan_id") or "") for entry in partition}

    queue = [
        row for row in _queue_view(state) if row["plan_id"] not in fix_ids
    ]
    running = [
        row
        for row in _running_view(state, now, min_runtime)
        if row["plan_id"] not in fix_ids
    ]
    parked = [
        row for row in _parked_view(state) if row["plan_id"] not in fix_ids
    ]
    waiting = [
        row for row in _waiting_view(state, now) if row["plan_id"] not in fix_ids
    ]

    return {
        "capacity": {"total": total, "used": used, "source": source},
        "queue_length": len(queue),
        "over_capacity": used > total,
        "queue": queue,
        "running": running,
        "waiting": waiting,
        # The plans parked for quota — the facts' answer.  A state that
        # names none is an empty list: nothing parked is a normal thing
        # to look at, and a list is an answer a caller can walk.
        "parked": parked,
        # The framework-fix lane's own plans — the panel's other column.
        # The key is always there: no repair plan is an empty list,
        # not a missing key.
        FRAMEWORK_FIXES_KEY: partition,
        # The plans that are stopping — the commands the scheduler has
        # accepted and not answered yet.  A state that names none is an
        # empty list: nothing is stopping is a normal thing to look at.
        "stopping": _stopping_view(_open_command_rows(state), now),
        "generated_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# The terminal summary
# ---------------------------------------------------------------------------


def postprocess_summary(
    plan_id: str,
    *,
    plans_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """The terminal summary of a plan: what the finishing stage decided.

    The summary is three of the terminal report's own fields — the
    fingerprint of the requirement book the verdicts were judged
    against, the stage's terminal status and the items' verdicts.  It
    is read straight out of ``postprocess_report.json``, so the panel's
    terminal area and the report cannot tell the operator two different
    stories.

    Args:
        plan_id: The plan the summary belongs to.
        plans_dir: The plans root.  Defaults to the report module's own
            default, which honours the ``PDT_PLANS_DIR`` escape hatch.

    Returns:
        The summary — ``spec_sha256``, ``terminal_status`` and
        ``item_verdicts`` — or ``None`` when the plan has no report,
        which means it never reached the finishing stage.  "No report"
        is an ordinary state, not an error.

    This is a **read**: it starts no subprocess, opens no database and
    writes nothing.
    """
    report = read_report(plan_id, plans_dir)
    if report is None:
        return None

    return {
        "spec_sha256": report["spec_sha256"],
        "terminal_status": report["terminal_status"],
        "item_verdicts": list(report["items"]),
    }


def with_postprocess(
    entry: Mapping[str, Any],
    *,
    plans_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """The plan entry, carrying the terminal summary when the plan has one.

    The terminal plan area shows plan entries that already exist, and
    the summary is added to the entry rather than kept beside it: the
    panel reads one entry per plan, and a summary that lived somewhere
    else would be a second place to look for the same answer.

    Args:
        entry: The plan entry as the caller already has it.
        plans_dir: The plans root — see :func:`postprocess_summary`.

    Returns:
        A copy of the entry.  When the plan has a terminal report, the
        copy carries a ``postprocess`` key with the summary; when it has
        none, the entry is handed back unchanged — a plan that never
        reached the finishing stage has no terminal result to show.
    """
    plan_id = str(_field(entry, "plan_id", "") or _field(entry, "id", "") or "")
    result = dict(entry)

    if not plan_id:
        return result

    summary = postprocess_summary(plan_id, plans_dir=plans_dir)
    if summary is not None:
        result["postprocess"] = summary

    return result