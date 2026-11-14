"""Zero-progress detection for the dispatch loop.

The dispatch loop works one *layer* at a time: it picks the tasks that
are ready, hands them to the executor, and comes back for the next
layer.  The outer loop reacts to a finished layer by dispatching the
next one *immediately*, which means a layer that ends with the task list
exactly as it started is indistinguishable, from the outside, from a
layer that did a great deal of work.

That difference is the whole point of this module.  A layer can end
without touching any task — an exception raised while a task is being
prepared, a provider that refuses before the executor starts, a
subtask that is written and then rolled back — and the loop that sees
an unchanged task list dispatches exactly the same work again, at
exactly the same speed.  The run stays alive, produces nothing, and
leaves nothing in the state to show that it happened.  A guard that
counts these layers and eventually says *stop* is what turns that spin
into a finished run with a named reason.

What counts as progress
-----------------------

A snapshot (:func:`task_progress_snapshot`) records the fields that
mean durable work: which tasks exist, what state each is in, and what
it committed.  Nothing else.

The exclusion is deliberate.  A field that is rewritten with the same
meaning — a timestamp, a retry counter, a re-worded error message — is
not progress, and a snapshot that counted it would let a spin look
busy: every layer would move the counter and the guard would never
fire.  The same exclusion runs the other way: a layer that *finishes* a
task has moved it out of the ready set and written a commit, and both
of those are in the snapshot, so real work is never mistaken for a
quiet layer.

The three answers
-----------------

:meth:`ZeroProgressGuard.observe`
    Did this layer change anything?  Two identical snapshots are a
    zero-progress layer and the counter goes up; two different ones are
    progress, and the counter goes back to zero — a run that is making
    progress is allowed an occasional quiet layer, and the count that
    decides when to give up has to forget the quiet layers that came
    before the progress.

:meth:`ZeroProgressGuard.backoff_delay`
    How long to wait before the next layer.  A zero-progress layer is
    not a reason to hammer the provider: the wait doubles each time
    (5s, 10s, 20s, ...) up to :data:`MAX_ZERO_PROGRESS_BACKOFF_SECONDS`,
    because the layers that follow a broken one are likely to be broken
    too, and a fast retry turns one incident into a thousand requests.

:meth:`ZeroProgressGuard.should_stop`
    Is it time to give up.  After
    :data:`DEFAULT_MAX_ZERO_PROGRESS_LAYERS` consecutive zero-progress
    layers the guard says stop, and the caller records
    :data:`ZERO_PROGRESS_STOP_REASON` — a named reason, so that a run
    which stopped can be told apart from a run that finished and from a
    run that stopped because a person was needed.

The knobs
---------

Both numbers are named, and both can be set from the environment
(:data:`ENV_MAX_ZERO_PROGRESS_LAYERS`,
:data:`ENV_ZERO_PROGRESS_BACKOFF_SECONDS`) so a deployment can tighten
them without a code change.  They are read when they are asked for, so
a deployment that lowers the limit mid-run is obeyed without a
restart.

An illegal value is not obeyed.  A limit of zero layers would stop the
run before its first layer, which is not a limit but a broken
configuration, and a negative wait is not "a very long wait" but a
mistake; both fall back to the documented default.  A backoff of zero
*is* legal — it means "do not wait", which is what a caller that wants
the guard without the wait asks for.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Iterable, Mapping
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: How many consecutive layers may finish without changing any task
#: state before the run is stopped.
DEFAULT_MAX_ZERO_PROGRESS_LAYERS = 3

#: The first wait a zero-progress layer takes before the next one.
DEFAULT_ZERO_PROGRESS_BACKOFF_SECONDS = 5.0

#: The ceiling on that wait, however many zero-progress layers pile up.
MAX_ZERO_PROGRESS_BACKOFF_SECONDS = 60.0

#: The reason recorded when the guard gives up.  Named, so a run that
#: stopped this way can be told apart from a run that finished.
ZERO_PROGRESS_STOP_REASON = "zero_progress"

#: The environment override for the layer limit.
ENV_MAX_ZERO_PROGRESS_LAYERS = "PDT_ZERO_PROGRESS_MAX_LAYERS"

#: The environment override for the first backoff.
ENV_ZERO_PROGRESS_BACKOFF_SECONDS = "PDT_ZERO_PROGRESS_BACKOFF_SECONDS"

#: The task fields a snapshot reads: which task it is, what state it is
#: in, and what it committed.
SNAPSHOT_FIELDS = ("id", "status", "commit_sha")


def _task_field(task: Any, name: str, default: Any = "") -> Any:
    """Read one field off a task, whatever shape the task arrives in.

    The dispatcher hands around :class:`task.SubTask` objects, but a
    caller that reads a task row out of the database has a mapping, and
    a caller that writes a fixture has a dict.  A snapshot that only
    understood one of them would silently read every field of the
    others as absent — and two task lists that differ would snapshot
    identically, which is the one answer this module must never give.
    """
    if isinstance(task, Mapping):
        value = task.get(name, default)
    else:
        value = getattr(task, name, default)
    return default if value is None else value


def task_progress_snapshot(
    tasks: Optional[Iterable[Any]],
) -> Tuple[Tuple[str, str, str], ...]:
    """What the task list looks like right now, in a comparable form.

    The result is a sorted tuple of ``(id, status, commit_sha)``
    records, so two snapshots can be compared with ``==`` and a
    snapshot can be put in a log or a dict key.  Sorting matters: the
    order the tasks come back in is not part of the work, and a
    snapshot that reported order would call a reordered list a change.

    ``None`` and an empty list are the same snapshot — no tasks is no
    tasks — and a task with a missing field is a task whose field is
    empty, which is what a task that has not run yet looks like.
    """
    records: List[Tuple[str, str, str]] = []
    for task in tasks or ():
        if task is None:
            continue
        records.append(
            (
                str(_task_field(task, "id")),
                str(_task_field(task, "status")),
                str(_task_field(task, "commit_sha")),
            )
        )
    return tuple(sorted(records))


def _parse_positive_int(value: Any) -> Optional[int]:
    """Read a count of layers, or ``None`` when the value is not one.

    A count has to be a whole number above zero.  A float that is not
    whole is not a count, and neither is a boolean — ``True`` is an
    ``int`` in Python, but "one layer, please" is not what a
    configuration file means by it.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        parsed = int(value)
    else:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
    return parsed if parsed > 0 else None


def _parse_non_negative_float(value: Any) -> Optional[float]:
    """Read a number of seconds, or ``None`` when the value is not one.

    Zero is a value here: "do not wait" is a wait of zero seconds, not
    an unreadable setting.  A negative number is not a long wait, it is
    a mistake, and ``inf`` / ``nan`` are not waits at all.
    """
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def resolve_max_zero_progress_layers(explicit: Optional[int] = None) -> int:
    """How many zero-progress layers are allowed before the run stops.

    An explicit value wins over the environment.  An illegal value
    loses to :data:`DEFAULT_MAX_ZERO_PROGRESS_LAYERS` either way: a
    limit of zero layers would stop the run before its first layer, so
    obeying it is not a stricter policy but a broken one.
    """
    if explicit is not None:
        parsed = _parse_positive_int(explicit)
        return parsed if parsed is not None else DEFAULT_MAX_ZERO_PROGRESS_LAYERS

    raw = os.environ.get(ENV_MAX_ZERO_PROGRESS_LAYERS)
    if raw is None:
        return DEFAULT_MAX_ZERO_PROGRESS_LAYERS
    parsed = _parse_positive_int(raw)
    return parsed if parsed is not None else DEFAULT_MAX_ZERO_PROGRESS_LAYERS


def resolve_zero_progress_backoff_seconds(explicit: Optional[float] = None) -> float:
    """The wait after the first zero-progress layer.

    An explicit value wins over the environment, and an illegal value
    loses to :data:`DEFAULT_ZERO_PROGRESS_BACKOFF_SECONDS`.  Zero is
    legal: it means the caller wants the guard's verdict without the
    wait.
    """
    if explicit is not None:
        parsed = _parse_non_negative_float(explicit)
        return (
            parsed if parsed is not None else DEFAULT_ZERO_PROGRESS_BACKOFF_SECONDS
        )

    raw = os.environ.get(ENV_ZERO_PROGRESS_BACKOFF_SECONDS)
    if raw is None:
        return DEFAULT_ZERO_PROGRESS_BACKOFF_SECONDS
    parsed = _parse_non_negative_float(raw)
    return parsed if parsed is not None else DEFAULT_ZERO_PROGRESS_BACKOFF_SECONDS


class ZeroProgressGuard:
    """Counts the layers that changed nothing, and knows when to stop.

    The guard is a plain counter with a wait attached.  It reads no
    clock and touches no file: the caller owns the layer boundary and
    hands in the two snapshots that bracket it.

    Attributes:
        zero_progress_layers: How many consecutive layers have changed
            nothing.  Reset to zero by a layer that did change
            something.
        max_zero_progress_layers: The limit above which
            :meth:`should_stop` answers yes.  Falls back to
            :data:`DEFAULT_MAX_ZERO_PROGRESS_LAYERS` when the
            configured value is illegal.
        backoff_seconds: The wait after the first zero-progress layer.
            Falls back to :data:`DEFAULT_ZERO_PROGRESS_BACKOFF_SECONDS`
            when the configured value is illegal; zero is legal.
    """

    def __init__(
        self,
        max_zero_progress_layers: Optional[int] = None,
        backoff_seconds: Optional[float] = None,
    ) -> None:
        self._max_layers = max_zero_progress_layers
        self._backoff_seconds = backoff_seconds
        self.zero_progress_layers = 0

        if (
            max_zero_progress_layers is not None
            and _parse_positive_int(max_zero_progress_layers) is None
        ):
            logger.warning(
                "zero-progress guard: ignoring invalid max_zero_progress_layers=%r;"
                " using %s",
                max_zero_progress_layers,
                DEFAULT_MAX_ZERO_PROGRESS_LAYERS,
            )
        if (
            backoff_seconds is not None
            and _parse_non_negative_float(backoff_seconds) is None
        ):
            logger.warning(
                "zero-progress guard: ignoring invalid backoff_seconds=%r; using %s",
                backoff_seconds,
                DEFAULT_ZERO_PROGRESS_BACKOFF_SECONDS,
            )

    @property
    def max_zero_progress_layers(self) -> int:
        """How many consecutive quiet layers are allowed before stopping."""
        return resolve_max_zero_progress_layers(self._max_layers)

    @max_zero_progress_layers.setter
    def max_zero_progress_layers(self, value: Optional[int]) -> None:
        self._max_layers = value

    @property
    def backoff_seconds(self) -> float:
        """The wait after the first quiet layer, in seconds."""
        return resolve_zero_progress_backoff_seconds(self._backoff_seconds)

    @backoff_seconds.setter
    def backoff_seconds(self, value: Optional[float]) -> None:
        self._backoff_seconds = value

    def observe(self, previous: Any, current: Any) -> bool:
        """Record one layer's outcome.

        Returns ``True`` when the layer changed nothing — the two
        snapshots are the same — and ``False`` when it changed
        something.  A changed snapshot clears the counter: the run is
        making progress, so the quiet layers that came before it no
        longer count against the limit.
        """
        if previous == current:
            self.zero_progress_layers += 1
            return True
        self.zero_progress_layers = 0
        return False

    def should_stop(self) -> bool:
        """Has the run been quiet for too many layers in a row?"""
        return self.zero_progress_layers >= self.max_zero_progress_layers

    def backoff_delay(self) -> float:
        """How long to wait before the next layer, in seconds.

        Zero while the run is making progress — there is nothing to
        wait out.  After a quiet layer the wait doubles per quiet layer
        and stops at :data:`MAX_ZERO_PROGRESS_BACKOFF_SECONDS`, because
        an unbounded wait is a run that never comes back.
        """
        layers = self.zero_progress_layers
        if layers <= 0:
            return 0.0

        delay = float(self.backoff_seconds)
        for _ in range(min(layers - 1, 64)):
            delay *= 2
            if delay >= MAX_ZERO_PROGRESS_BACKOFF_SECONDS:
                return MAX_ZERO_PROGRESS_BACKOFF_SECONDS
        return min(delay, MAX_ZERO_PROGRESS_BACKOFF_SECONDS)

    def reset(self) -> None:
        """Forget the quiet layers, as if the guard had just been made."""
        self.zero_progress_layers = 0
