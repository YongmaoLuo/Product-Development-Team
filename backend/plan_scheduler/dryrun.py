"""backend/plan_scheduler.dryrun — where a tick's decisions go.

Background
----------
The scheduler runs **one** tick pipeline against **one** policy.  That
pipeline produces a list of *decisions* — "admit this plan", "leave that
one alone", "stop the third" — and what happens to those decisions is a
question of its own.  The dry run and the live engine have to answer it
the same way: the same tick code, the same policy, the same decisions.
Only the exit differs.  A dry run that decided differently would say
nothing about the live engine, which is the whole reason there is one.

So the decisions leave the pipeline through a single interface — the
*sink* — and there are two of them:

* :class:`ExecuteSink` hands every decision to an injected executor:
  the live path, where an executor starts or stops a plan.
* :class:`RecordSink` writes every decision to a JSONL file: the dry-run
  path, where nothing else happens at all.

Both are a :class:`DecisionSink`, so the tick code holds one type and
never asks "are we dry right now?".  The contracts below are what make
the two exits comparable:

* **One decision list, two exits.**  The same list handed to both sinks
  yields the same ``action`` / ``plan_id`` / ``reason`` for every item.
* **Recording is inert.**  :class:`RecordSink` writes a file.  It never
  calls the plan lifecycle and never starts a subprocess.
* **The record accumulates.**  The file is appended to, so one run's
  decisions sit next to the earlier runs' instead of replacing them.
* **Nothing to say, nothing written.**  An empty decision list writes
  no file, creates no directory and raises nothing.

Verdict — whether the dry run allows the switch
----------------------------------------------
The sinks say what the new engine *decided*.  The verdict says whether
the new engine and the old one *behave the same*, which is the question
the switch actually turns on.  A 24 hour dry run compares the two
across 48 half-hour windows, and the criterion is written down once
and then executed:

* at least 45 of the 48 windows are **identical**;
* the windows that are not identical may only contain **allowed**
  differences;
* there is **no forbidden difference** anywhere;
* every window was **compared**.  A window without a comparison side
  is not evidence of anything: it is counted as *uncompared*, and a
  run that compared nothing may not switch.

Two pure functions carry it: :func:`evaluate_windows` turns the
windows into a :class:`Verdict`, and :func:`classify_difference` says
whether one difference is allowed or forbidden.  Neither reads a file
or touches the scheduler, so the criterion is checkable in a unit test
and not only after a real 24 hour run.  A difference kind nobody
classified counts as forbidden: a missed forbidden difference would
switch on bad evidence, while a false alarm only costs another run.

The evidence itself
-------------------
The verdict needs evidence, and the evidence is what a dry run leaves
behind: three files in the day's directory under the dry-run root —
``ticks.jsonl`` (the decisions), ``windows.json`` (the half-hour
windows the comparison is made in) and ``diff.md`` (the report over
them).  :class:`DryRunWriter` is what writes them.

The writer is the dry run's *own* bookkeeping, and it is written the
same way a live run writes it: the plan facts are read and never
written, while the scheduler's own facts — the queue, the slots — are
written as usual.  Reading "dry run" as "write nothing" would lose the
plan's priority and its place in the line, and a scheduler that lost
its place would come back after a restart with a different line than
the one that was compared.

The comparison side
-------------------
A window on its own says nothing about whether the two engines agree:
the writer holds the **new** engine's side of the comparison, and the
other side is the old engine's already-written-down record of the same
period.  That record is read and never written — it is evidence, and
evidence that the comparison itself writes is no longer evidence.

A window whose other side is missing is recorded as **not compared**,
never as "identical".  Writing "identical" into a window that was
never compared is how a dry run that compared nothing at all would
come out of the gate as a perfect score.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from collections.abc import Sequence as AbcSequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "DECISIONS_FILENAME",
    "RECORD_FIELDS",
    "ALLOWED",
    "FORBIDDEN",
    "IDENTICAL",
    "ALLOWED_ONLY",
    "UNCOMPARED",
    "MIN_WINDOWS",
    "MIN_CONSISTENT",
    "DIFF_TRANSIENT_SET",
    "DIFF_WORKSPACE_ORDER",
    "DIFF_NEW_START",
    "DIFF_EVICTION_FLIP",
    "DIFF_QUEUE_ORDER_FLIP",
    "ALLOWED_DIFFERENCE_KINDS",
    "FORBIDDEN_DIFFERENCE_KINDS",
    "TICKS_FILENAME",
    "WINDOWS_FILENAME",
    "DIFF_FILENAME",
    "DEFAULT_DRYRUN_ROOT",
    "WINDOW_MINUTES",
    "WINDOW_COMPARED_KEY",
    "WINDOW_REFERENCE_KEY",
    "DecisionSink",
    "ExecuteSink",
    "RecordSink",
    "DryRunWriter",
    "ReferenceWindows",
    "Verdict",
    "classify_difference",
    "classify_window",
    "compare_windows",
    "evaluate_windows",
]

#: The file a :class:`RecordSink` appends to, inside the directory it
#: was given.
DECISIONS_FILENAME = "decisions.jsonl"

#: The fields a recorded decision is made of.  The architecture's
#: decision point 7 pins the record's shape: the sink appends exactly
#: these four, so a record is the comparable core of a decision and
#: nothing else.
RECORD_FIELDS = ("ts", "action", "plan_id", "reason")


class DecisionSink(ABC):
    """Where a tick's decisions go.

    The interface the tick pipeline hands its decisions to.  It has one
    method, :meth:`apply`, because the pipeline's question is always the
    same — "here is what I decided; do something with it" — and the
    answer is the only thing that differs between a dry run and a live
    engine.

    Implementations return one entry per decision, in the order the
    decisions were given, so the caller can line the two exits up
    against each other.
    """

    @abstractmethod
    def apply(self, decisions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Take the tick's decisions and return what each one produced.

        Args:
            decisions: the decisions the tick pipeline produced.  Each
                one is a mapping carrying at least an ``action``, a
                ``plan_id`` and a ``reason``.

        Returns:
            One entry per decision, in the order the decisions came in.
        """


class ExecuteSink(DecisionSink):
    """Hand every decision to an executor, in order.

    This is the live exit.  The executor is injected rather than
    imported, so the sink never grows an opinion about *how* a plan is
    started or stopped — that stays with the caller, and the scheduler
    package does not have to know the plan lifecycle exists.

    The executor is handed the decision itself, not a copy: the
    decision list a tick produced is its own, and the executor is
    trusted to read it rather than to edit it.  An executor that
    rewrites what it is given would change what the recording exit
    sees, and the two exits would no longer be comparable.

    An executor that raises ends the whole call: a decision the
    executor could not carry out is not a decision that was carried
    out, and hiding that behind a placeholder would make the live exit
    look like it succeeded.
    """

    def __init__(self, executor: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        """Remember the executor the decisions are handed to.

        Args:
            executor: called once per decision, with the decision, and
                expected to return a mapping describing what it did.
        """
        if not callable(executor):
            raise TypeError("executor must be callable")
        self._executor = executor

    @property
    def executor(self) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        """The executor this sink hands its decisions to."""
        return self._executor

    def apply(self, decisions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Run every decision through the executor, in order.

        Returns:
            The executor's results, one per decision, in order.
        """
        return [self._executor(decision) for decision in decisions]


class RecordSink(DecisionSink):
    """Write every decision to a JSONL file, and touch nothing else.

    This is the dry-run exit, and it is deliberately inert: it writes
    the decisions down and does nothing else.  It does not call the
    plan lifecycle, it does not start a subprocess, and it does not
    read the state of anything.  A dry run that could change the world
    would not be a dry run, and a record sink that did could not be
    compared against the live engine.

    The file is opened in append mode, so the decisions of one run sit
    next to those of the earlier runs instead of replacing them, and the
    directory is created on demand — the first time there is something
    to write.

    A record is the four canonical fields — ``ts``, ``action``,
    ``plan_id`` and ``reason`` — and nothing else, as the
    architecture's decision point 7 pins it.  The record is the
    comparable core of a decision: what the live engine acted on and
    what the dry run wrote down can be lined up against each other.
    """

    def __init__(self, out_dir: os.PathLike[str] | str) -> None:
        """Remember the directory the decisions are written to.

        Args:
            out_dir: the directory the JSONL file lives in.  It is
                created when the sink first writes, not here, so that a
                tick with nothing to say leaves no trace.
        """
        self._out_dir = Path(out_dir)

    @property
    def out_dir(self) -> Path:
        """The directory this sink writes into."""
        return self._out_dir

    @property
    def path(self) -> Path:
        """The file this sink appends to."""
        return self._out_dir / DECISIONS_FILENAME

    def apply(self, decisions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Append one JSONL line per decision.

        Args:
            decisions: the decisions to record.

        Returns:
            The records that were written, in order.  An empty list
            writes nothing and returns nothing.
        """
        if not decisions:
            return []

        records = [self._as_record(decision) for decision in decisions]
        self._out_dir.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(self._as_line(record))
        return records

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _as_record(decision: Dict[str, Any]) -> Dict[str, Any]:
        """Return the recordable part of one decision.

        A record is the four canonical fields and nothing else, so two
        runs over the same decisions write byte-identical lines: the
        record is meant to be diffed.  A field the decision does not
        carry is written as ``null`` rather than dropped, so every line
        has the same shape.
        """
        return {field: decision.get(field) for field in RECORD_FIELDS}

    @staticmethod
    def _as_line(record: Dict[str, Any]) -> str:
        """Return one record as a single JSONL line."""
        return json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# The verdict: does the dry run let the switch happen?
# ---------------------------------------------------------------------------

#: The number of windows a dry run has to cover.  The old scheduler ran
#: on a 30 minute cron window, and 24 hours of it is 48 windows — the
#: unit the two schedulers are compared in.
MIN_WINDOWS = 48

#: The number of windows that have to be identical for the dry run to
#: count as "the two schedulers behave the same".
MIN_CONSISTENT = 45

#: The two answers :func:`classify_difference` gives.
ALLOWED = "allowed"
FORBIDDEN = "forbidden"

#: The answer one window gets when the two engines were compared and
#: agreed on everything.
IDENTICAL = "identical"

#: The answer one window gets when the two engines were compared and
#: every difference they found is an allowed one.
ALLOWED_ONLY = "allowed_only"

#: The answer one window gets when it was **not** compared at all.
#: Such a window is not evidence: it agrees with nothing, because
#: nobody looked.
UNCOMPARED = "uncompared"

#: The key a window record carries to say that it was compared.  A
#: window without it — and without a reference payload — was not
#: compared, and is counted as such.
WINDOW_COMPARED_KEY = "compared"

#: The key a window record may carry the comparison side under.  A
#: window that carries one was compared: the comparison is right
#: there in the record.
WINDOW_REFERENCE_KEY = "reference"

#: The set of running plans differs, because the two engines tick on
#: slightly different rhythms and a plan can be running in one window
#: and not in the other.  Allowed.
DIFF_TRANSIENT_SET = "transient_set_diff"

#: The order in which two workspaces were served differs, because one
#: workspace held the other up.  Allowed.
DIFF_WORKSPACE_ORDER = "workspace_order_diff"

#: The new engine started a plan the old engine never started.  Not
#: allowed: the new engine admits work the old one did not.
DIFF_NEW_START = "new_start"

#: The two engines disagreed about whether a plan was evictable.  Not
#: allowed: one of them would have thrown away a plan the other kept.
DIFF_EVICTION_FLIP = "eviction_flip"

#: The two engines queued the plans in the opposite order.  Not allowed:
#: the order is what the plan's queue position means.
DIFF_QUEUE_ORDER_FLIP = "queue_order_flip"

#: The difference kinds the architecture calls allowed.
ALLOWED_DIFFERENCE_KINDS = frozenset(
    {DIFF_TRANSIENT_SET, DIFF_WORKSPACE_ORDER}
)

#: The difference kinds the architecture calls forbidden.
FORBIDDEN_DIFFERENCE_KINDS = frozenset(
    {DIFF_NEW_START, DIFF_EVICTION_FLIP, DIFF_QUEUE_ORDER_FLIP}
)

#: Other spellings of the six kinds above — the same six classes, written
#: differently.  They are matched exactly, so a near miss is *not* a
#: synonym: an unrecognised spelling has to fall through to
#: :data:`FORBIDDEN` instead of quietly becoming allowed.
_KIND_ALIASES: Dict[str, str] = {
    "transient_set_difference": DIFF_TRANSIENT_SET,
    "transient_running_set_diff": DIFF_TRANSIENT_SET,
    "transient_running_set_difference": DIFF_TRANSIENT_SET,
    "running_set_diff": DIFF_TRANSIENT_SET,
    "workspace_order_difference": DIFF_WORKSPACE_ORDER,
    "workspace_ordering_diff": DIFF_WORKSPACE_ORDER,
    "workspace_order": DIFF_WORKSPACE_ORDER,
    "new_start_diff": DIFF_NEW_START,
    "new_only_start": DIFF_NEW_START,
    "started_only_by_new": DIFF_NEW_START,
    "eviction_conflict": DIFF_EVICTION_FLIP,
    "eviction_opposite": DIFF_EVICTION_FLIP,
    "eviction_disagreement": DIFF_EVICTION_FLIP,
    "eviction_mismatch": DIFF_EVICTION_FLIP,
    "queue_order_conflict": DIFF_QUEUE_ORDER_FLIP,
    "queue_order_inverted": DIFF_QUEUE_ORDER_FLIP,
    "queue_order_opposite": DIFF_QUEUE_ORDER_FLIP,
    "queue_order_reversed": DIFF_QUEUE_ORDER_FLIP,
}

#: The keys a window may carry its differences under.  The first one
#: present wins.
_DIFFERENCE_KEYS = (
    "differences",
    "diffs",
    "difference",
    "diff",
    "diff_kinds",
    "kinds",
)


@dataclass(frozen=True)
class Verdict:
    """What a dry run says about the switch.

    The four counts partition the windows that were judged:

    * ``consistent`` — the two engines were compared and were
      identical in this window;
    * ``allowed_only`` — they were compared, they differed, and every
      difference was one the architecture calls allowed;
    * ``forbidden`` — they were compared and differed in a way that is
      not allowed;
    * ``uncompared`` — nobody compared this window, so it says
      nothing about the two engines.

    ``passed`` is the answer the switch turns on: at least
    :data:`MIN_CONSISTENT` of at least :data:`MIN_WINDOWS` windows
    identical, no forbidden difference anywhere, and no window left
    uncompared.  A run that compared nothing has no consistent window
    to count, and no uncompared window is allowed to hide in the
    middle of the day.
    """

    passed: bool
    consistent: int
    allowed_only: int
    forbidden: int
    uncompared: int = 0


def classify_difference(kind: Any) -> str:
    """Say whether one difference is allowed or forbidden.

    Args:
        kind: the difference's kind.  Either the kind itself — a
            string — or a difference entry carrying one under
            ``kind`` (or ``type``).

    Returns:
        :data:`ALLOWED` or :data:`FORBIDDEN`.

    The three allowed kinds are a rhythm-driven difference in the set of
    running plans and a difference in the order two workspaces were
    served in.  The three forbidden kinds are the new engine starting a
    plan the old engine never started, the two engines disagreeing
    about whether a plan was evictable, and the two engines queueing
    the plans in the opposite order.

    Anything else is forbidden.  A kind nobody classified is not a
    difference that does not matter — it is a difference nobody has
    looked at, and the cost of treating it as harmless is switching on
    bad evidence.
    """
    if isinstance(kind, Mapping):
        for key in ("kind", "type"):
            value = kind.get(key)
            if isinstance(value, str):
                kind = value
                break
        else:
            return FORBIDDEN

    if not isinstance(kind, str):
        return FORBIDDEN

    name = kind.strip().lower()
    if name in ALLOWED_DIFFERENCE_KINDS:
        return ALLOWED
    if name in FORBIDDEN_DIFFERENCE_KINDS:
        return FORBIDDEN

    canonical = _KIND_ALIASES.get(name)
    if canonical is not None:
        return ALLOWED if canonical in ALLOWED_DIFFERENCE_KINDS else FORBIDDEN
    return FORBIDDEN


def classify_window(window: Any) -> str:
    """Say what one window is: identical, allowed-only, forbidden or uncompared.

    Args:
        window: one window record.

    Returns:
        :data:`IDENTICAL`, :data:`ALLOWED_ONLY`, :data:`FORBIDDEN` or
        :data:`UNCOMPARED`.

    A window that lists a difference was compared — somebody looked
    and found something — and it is judged by what it found.  A
    window that lists no difference is identical only if it says it
    was compared: the comparison is what found the agreement, and a
    window that was never compared agrees with nothing.  A window that
    says it differs and names no difference is forbidden: the
    difference is unexplained, and an unexplained difference is never
    quietly allowed.
    """
    entries, declared = _window_differences(window)
    kinds = [_difference_kind(entry) for entry in entries]

    if kinds:
        if all(classify_difference(kind) == ALLOWED for kind in kinds):
            return ALLOWED_ONLY
        return FORBIDDEN

    if declared is False:
        return FORBIDDEN

    if _was_compared(window):
        return IDENTICAL
    return UNCOMPARED


def _was_compared(window: Any) -> bool:
    """Whether this window was compared with the old engine.

    The evidence is either the record's own word for it — the
    ``compared`` flag the writer writes — or the comparison side
    itself, which is right there in the record.
    """
    if not isinstance(window, Mapping):
        return False
    if window.get(WINDOW_COMPARED_KEY) is True:
        return True
    return bool(window.get(WINDOW_REFERENCE_KEY))


def evaluate_windows(windows: Iterable[Any]) -> Verdict:
    """Judge a dry run's windows and say whether the switch may happen.

    Args:
        windows: the windows the dry run covered, oldest first.  A
            window says what differed between the two engines, either
            as a list of differences (each one a mapping carrying a
            ``kind``, or the kind itself as a string) or as a bare list
            of kinds; a window that says it was identical may also say
            so outright, with ``consistent``.

    Returns:
        A :class:`Verdict` — the counts of the four kinds of window
        and whether the dry run passed.

    The rule is the one the architecture pins: at least
    :data:`MIN_CONSISTENT` of at least :data:`MIN_WINDOWS` windows
    identical, every other window allowed-only, no forbidden
    difference anywhere, and no window left uncompared.  A window that
    carries a forbidden difference is counted as forbidden even if it
    also carries an allowed one — "allowed only" means *every*
    difference it carries is allowed.

    A window that was never compared is counted as uncompared, not as
    consistent: the writer holds one side of the comparison, and a
    window with no other side says nothing about whether the two
    engines agree.
    """
    total = 0
    consistent = 0
    allowed_only = 0
    forbidden = 0
    uncompared = 0

    for window in windows:
        total += 1
        conclusion = classify_window(window)
        if conclusion == IDENTICAL:
            consistent += 1
        elif conclusion == ALLOWED_ONLY:
            allowed_only += 1
        elif conclusion == FORBIDDEN:
            forbidden += 1
        else:
            uncompared += 1

    passed = (
        total >= MIN_WINDOWS
        and consistent >= MIN_CONSISTENT
        and forbidden == 0
        and uncompared == 0
    )
    return Verdict(
        passed=passed,
        consistent=consistent,
        allowed_only=allowed_only,
        forbidden=forbidden,
        uncompared=uncompared,
    )


def _window_differences(window: Any) -> Tuple[List[Any], Optional[bool]]:
    """Return one window's difference entries and its consistency flag.

    The flag is a fallback, not an override: a window that lists
    differences is judged by those differences, and only a window with
    no differences at all falls back to the flag.
    """
    if isinstance(window, Mapping):
        entries: List[Any] = []
        for key in _DIFFERENCE_KEYS:
            value = window.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                entries = [value]
            else:
                entries = list(value)
            break
        declared = window.get("consistent")
        return entries, declared if isinstance(declared, bool) else None

    if isinstance(window, str):
        return [window], None

    if isinstance(window, AbcSequence):
        return list(window), None

    return [], None


def _difference_kind(entry: Any) -> str:
    """Return the kind of one difference entry.

    An entry that carries no kind at all comes back as the empty
    string, which classifies as forbidden — a difference nobody could
    name is not a difference that does not matter.
    """
    if isinstance(entry, str):
        return entry

    if isinstance(entry, Mapping):
        for key in ("kind", "type"):
            value = entry.get(key)
            if isinstance(value, str):
                return value

    return ""


# ---------------------------------------------------------------------------
# The artifacts: what a dry run leaves behind
# ---------------------------------------------------------------------------

#: The file the tick decisions are appended to, inside the day's
#: directory.
TICKS_FILENAME = "ticks.jsonl"

#: The file the window records are written to.
WINDOWS_FILENAME = "windows.json"

#: The file the report over those windows is written to.
DIFF_FILENAME = "diff.md"

#: The directory the dry-run artifacts are written under, when the
#: caller names no other place.  The day's directory is created under
#: it, so one day of a dry run is one directory.
DEFAULT_DRYRUN_ROOT = "/tmp/pdt-native-scheduler/dryrun"

#: The length of one comparison window, in minutes: the old scheduler's
#: cron period, and the unit the two schedulers are compared in.
WINDOW_MINUTES = 30


def _stamp(epoch: float) -> str:
    """Return the moment the way the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


# ---------------------------------------------------------------------------
# The comparison side: the old engine's recorded windows
# ---------------------------------------------------------------------------


class ReferenceWindows:
    """The old engine's recorded windows — the comparison side, read only.

    The old engine wrote down what it did, one record per half-hour
    window, and those records are the other side of the comparison.
    They are read and never written: a comparison that wrote into the
    record it is judging would stop being evidence.

    The records have the same shape the dry run writes for itself —
    ``start``, ``end``, the running set and the queue — so the two
    sides can be lined up against each other window by window.  A
    record may carry two extras: the plans the old engine started in
    the window (``started``) and the order it served the workspaces
    in (``workspaces``).  Both are optional; what the comparison cannot
    answer, it does not answer.

    A window the reference has no record for is a window that was not
    compared.  It is not an error and it is not an empty agreement:
    it is simply missing, and the dry run says so.
    """

    def __init__(self, records: Any = None) -> None:
        """Take the old engine's records, or a reader that looks them up.

        Args:
            records: the records themselves — a list of window records,
                a mapping of window start to window record, or a
                callable that is handed a window start and answers with
                the record (or with nothing).
        """
        self._windows: Dict[str, Dict[str, Any]] = {}
        self._reader: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None

        if callable(records):
            self._reader = records
            return

        for record in _window_records(records):
            if not isinstance(record, Mapping):
                continue
            start = record.get("start")
            if not isinstance(start, str) or not start:
                continue
            self._windows[start] = dict(record)

    @classmethod
    def from_file(cls, path: os.PathLike[str] | str) -> "ReferenceWindows":
        """Read the old engine's records from a JSON file.

        The file holds either a list of window records or an object
        with the list under ``windows``, ``records`` or ``entries``.

        Args:
            path: the file the old engine's records live in.

        Returns:
            The records, read.

        Raises:
            OSError: the file cannot be read.
            ValueError: the file does not hold window records.
        """
        text = Path(path).read_text(encoding="utf-8")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("{0}: 不是合法的 JSON ({1})".format(path, exc)) from exc
        return cls(payload)

    def window_at(self, start: str) -> Optional[Dict[str, Any]]:
        """The old engine's record for the window that starts at ``start``.

        Args:
            start: the window's start, as the day directory's stamps it.

        Returns:
            The record, or ``None`` when the old engine has none.
        """
        if self._reader is not None:
            found = self._reader(start)
            return dict(found) if isinstance(found, Mapping) else None
        found = self._windows.get(start)
        return dict(found) if found is not None else None

    def __len__(self) -> int:
        """How many windows the reference holds."""
        return len(self._windows)


def _as_reference(reference: Any) -> Optional[ReferenceWindows]:
    """Return the comparison side, whatever shape it was handed in.

    Args:
        reference: a :class:`ReferenceWindows`, a list of window
            records, a mapping of window start to window record, a path
            to a JSON file holding them, or a callable that looks a
            window up.

    Returns:
        The comparison side, or ``None`` when there is none.
    """
    if reference is None:
        return None
    if isinstance(reference, ReferenceWindows):
        return reference
    if isinstance(reference, (str, bytes, os.PathLike)):
        return ReferenceWindows.from_file(reference)
    return ReferenceWindows(reference)


def _window_records(payload: Any) -> List[Any]:
    """Return the window records a payload holds.

    A payload is either the records themselves, an object with the
    list under ``windows`` / ``records`` / ``entries``, or a mapping
    of window start to window record.
    """
    if payload is None:
        return []
    if isinstance(payload, Mapping):
        for key in ("windows", "records", "entries"):
            value = payload.get(key)
            if isinstance(value, list):
                return list(value)
        return list(payload.values())
    if isinstance(payload, (str, bytes, os.PathLike)):
        return []
    return list(payload)


def _plan_ids(rows: Any) -> List[str]:
    """Return the plan ids a snapshot's rows carry, in their order.

    A row is either a mapping with a ``plan_id`` or the plan id
    itself, so a record written by either engine can be read.
    """
    if rows is None:
        return []
    if isinstance(rows, (str, bytes)):
        return [str(rows)]
    ids: List[str] = []
    for row in rows:
        if isinstance(row, Mapping):
            value = row.get("plan_id")
        else:
            value = row
        if value is None:
            continue
        ids.append(str(value))
    return ids


def _workspace_order(record: Mapping[str, Any]) -> List[str]:
    """Return the order in which a record's workspaces were served.

    The order is the record's own ``workspaces`` list when it has
    one; otherwise it is read off the queue rows, which may carry the
    workspace each plan works in.
    """
    order = record.get("workspaces")
    if isinstance(order, list):
        return [str(item) for item in order]
    workspaces: List[str] = []
    for row in record.get("queue") or ():
        if not isinstance(row, Mapping):
            continue
        value = row.get("workspace")
        if value is None:
            continue
        text = str(value)
        if text not in workspaces:
            workspaces.append(text)
    return workspaces


def _difference(kind: str, explanation: str) -> Dict[str, Any]:
    """Return one difference, with the kind and the reason for it."""
    return {"kind": kind, "explanation": explanation}


def compare_windows(
    current: Mapping[str, Any],
    reference: Mapping[str, Any],
    *,
    admitted: Sequence[str] = (),
    stopped: Sequence[str] = (),
    workspaces: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """Compare one window with the old engine's window for the same period.

    The two sides are the same two things: the set of plans that were
    running and the line of plans that were waiting.  What the
    comparison finds is named in the architecture's own vocabulary, and
    every difference it reports carries a written explanation — the
    criterion asks for the allowed differences to be explained one by
    one.

    Args:
        current: the new engine's window — its running set and its
            queue.
        reference: the old engine's record of the same window.
        admitted: the plans the new engine admitted up to this window.
        stopped: the plans the new engine stopped up to this window.
        workspaces: the order in which the new engine served the
            workspaces of the queued plans.

    Returns:
        The differences the comparison found.  A plan that only one
        side was running is a rhythm difference — unless the new
        engine is the side that started or stopped it, in which case
        the difference is named for what it is: a new start or an
        eviction the old engine did not make.
    """
    differences: List[Dict[str, Any]] = []

    current_running = _plan_ids(current.get("running"))
    reference_running = _plan_ids(reference.get("running"))
    admitted_set = {str(plan_id) for plan_id in admitted}
    stopped_set = {str(plan_id) for plan_id in stopped}

    for plan_id in current_running:
        if plan_id in reference_running:
            continue
        if plan_id in admitted_set:
            differences.append(
                _difference(
                    DIFF_NEW_START,
                    "新引擎启动了 {0}，旧引擎该周期没有它".format(plan_id),
                )
            )
        else:
            differences.append(
                _difference(
                    DIFF_TRANSIENT_SET,
                    "运行集不同：{0} 只在新引擎这一侧运行".format(plan_id),
                )
            )

    for plan_id in reference_running:
        if plan_id in current_running:
            continue
        if plan_id in stopped_set:
            differences.append(
                _difference(
                    DIFF_EVICTION_FLIP,
                    "新引擎停掉了 {0}，旧引擎该周期还在跑它".format(plan_id),
                )
            )
        else:
            differences.append(
                _difference(
                    DIFF_TRANSIENT_SET,
                    "运行集不同：{0} 只在旧引擎这一侧运行".format(plan_id),
                )
            )

    current_queue = _plan_ids(current.get("queue"))
    reference_queue = _plan_ids(reference.get("queue"))
    if current_queue != reference_queue:
        if set(current_queue) != set(reference_queue):
            differences.append(
                _difference(
                    DIFF_TRANSIENT_SET,
                    "队列成员不同：新引擎 [{0}]，旧引擎 [{1}]".format(
                        ", ".join(current_queue) or "-",
                        ", ".join(reference_queue) or "-",
                    ),
                )
            )
        current_set = set(current_queue)
        reference_set = set(reference_queue)
        common_current = [plan for plan in current_queue if plan in reference_set]
        common_reference = [plan for plan in reference_queue if plan in current_set]
        if common_current != common_reference:
            differences.append(
                _difference(
                    DIFF_QUEUE_ORDER_FLIP,
                    "队列顺序不同：新引擎 [{0}]，旧引擎 [{1}]".format(
                        ", ".join(common_current) or "-",
                        ", ".join(common_reference) or "-",
                    ),
                )
            )

    reference_workspaces = _workspace_order(reference)
    if reference_workspaces and list(workspaces) != list(reference_workspaces):
        differences.append(
            _difference(
                DIFF_WORKSPACE_ORDER,
                "工作区服务顺序不同：新引擎 [{0}]，旧引擎 [{1}]".format(
                    ", ".join(str(item) for item in workspaces) or "-",
                    ", ".join(reference_workspaces) or "-",
                ),
            )
        )

    return differences


class DryRunWriter(DecisionSink):
    """Write a dry run's three artifacts, and change nothing else.

    A dry run is *read-only about the plans* and *write-everything
    about itself*:

    * the plan facts — the phase, the running process, the pending
      decision — are read and never written.  A dry run that moved a
      plan's phase would be a live run wearing a hat;
    * the scheduler's own facts — the queue, the slots — are written
      exactly as they are in a live run.  They are what the scheduler
      needs in order to keep its place: a dry run that dropped the
      queue would lose the plan's priority and its position in the
      line, and a scheduler that lost its place would come back after a
      restart with a different line than the one that was compared.

    The three artifacts are written under ``out_dir``, in a directory
    named after the day — ``out_dir/YYYYMMDD``:

    * ``ticks.jsonl`` — one line per decision, the four canonical
      fields, appended to;
    * ``windows.json`` — the comparison windows: one record per
      half-hour boundary the run crossed, carrying the running set and
      the queue snapshot;
    * ``diff.md`` — the report over those windows.

    The windows are the comparison unit, and they are on the clock: a
    window starts at every half hour, the way the old scheduler's cron
    did.  A run that begins at 10:29 has been running inside the 10:00
    window, so that window is recorded too — the comparison has to see
    the state the run started in, not only the windows it began on.

    A second run on the same day adds to the day's artifacts instead of
    replacing them: the ticks are appended to, and a window that is
    already on record keeps the record it has.  A day of evidence is
    worth more than the last run's evidence.

    The writer is a :class:`DecisionSink`, so the tick hands its
    decisions to it the way it hands them to the executor — the same
    exit, a different place to put them.

    A window is only as good as the comparison behind it.  The writer
    holds the **new** engine's side; the other side is the old
    engine's already-written-down record of the same period, and it is
    read and never written.  A window whose other side is missing is
    recorded as *not compared* — never as "identical", because a
    window that was never compared agrees with nothing.
    """

    def __init__(
        self,
        out_dir: os.PathLike[str] | str = DEFAULT_DRYRUN_ROOT,
        *,
        clock: Optional[Callable[[], float]] = None,
        store: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        window_minutes: int = WINDOW_MINUTES,
        reference: Any = None,
        workspace_of: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        """Bind the writer to the place it writes to and to the world it reads.

        Args:
            out_dir: the directory the day's directory is created under.
                The day's directory is named after the day the clock is
                on, so the artifacts of one day sit together.
            clock: the source of "now", in epoch seconds.  It decides
                the day the artifacts belong to and the window
                boundaries they are recorded on, so a case that moves
                the clock by hand moves them with it.
            store: the scheduler's store — the reader of the queue and
                the slots, and therefore the source of the window's
                running set and queue snapshot.  Without it the
                windows carry empty snapshots.
            conn: the connection to read the scheduler's tables
                through.
            window_minutes: how long one window is.
            reference: the comparison side — the old engine's recorded
                windows.  A :class:`ReferenceWindows`, a list of window
                records, a mapping of window start to window record, a
                path to a JSON file holding them, or a callable that
                looks a window up.  Without it every window is
                recorded as *not compared*.
            workspace_of: the facts reader's ``workspace_key`` — what
                tells the writer which workspace a plan works in, and
                therefore the order the workspaces were served in.
        """
        self._root = Path(out_dir)
        self._clock = clock if clock is not None else time.time
        self._store = store
        self._conn = conn
        self._window_seconds = max(1, int(window_minutes) * 60)
        self._reference = _as_reference(reference)
        self._workspace_of = workspace_of
        self._windows: List[Dict[str, Any]] = []
        self._last_boundary: Optional[float] = None
        self._ticks = 0
        self._prior_ticks: Optional[int] = None
        self._admitted: List[str] = []
        self._stopped: List[str] = []

    # ------------------------------------------------------------------
    # Where the artifacts live
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """The directory the day's directory is created under."""
        return self._root

    @property
    def day_dir(self) -> Path:
        """The directory this day's artifacts are written to."""
        return self._root / self._day()

    @property
    def ticks_path(self) -> Path:
        """The file the tick decisions are appended to."""
        return self.day_dir / TICKS_FILENAME

    @property
    def windows_path(self) -> Path:
        """The file the window records are written to."""
        return self.day_dir / WINDOWS_FILENAME

    @property
    def diff_path(self) -> Path:
        """The file the report over the windows is written to."""
        return self.day_dir / DIFF_FILENAME

    @property
    def windows(self) -> List[Dict[str, Any]]:
        """The windows of this day — this run's and the earlier runs'."""
        return self._merged_windows()

    @property
    def ticks(self) -> int:
        """How many decisions this writer has written down."""
        return self._ticks

    @property
    def day_ticks(self) -> int:
        """How many decisions the day's file holds, all runs together."""
        return (self._prior_ticks or 0) + self._ticks

    # ------------------------------------------------------------------
    # The sink
    # ------------------------------------------------------------------

    def apply(self, decisions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Write the tick's decisions down, and close the windows it crossed.

        Args:
            decisions: the decisions the tick produced.  Each one
                carries the four canonical fields.

        Returns:
            The records that were written, in order.  A tick with
            nothing to say still closes the windows it crossed.
        """
        records = [self._as_record(decision) for decision in decisions]
        self._remember(records)
        if records:
            self._append_ticks(records)
        self._catch_up()
        if records:
            # The report is about the day's artifacts, so it is written
            # whenever the day's artifacts change: a report that is a
            # window behind says less than the files next to it.
            self._write_diff()
        return records

    # ------------------------------------------------------------------
    # The windows
    # ------------------------------------------------------------------

    def record_window(self, boundary: Optional[float] = None) -> Dict[str, Any]:
        """Record the window that starts at a boundary.

        The window carries the new engine's side of the comparison and
        the result of comparing it with the old engine's side.  A
        window the old engine has no record for is recorded as *not
        compared* — it is not recorded as identical, because nobody
        compared it with anything.

        Args:
            boundary: the moment the window starts, in epoch seconds.
                The boundary of the current moment when it is omitted.

        Returns:
            The window record that was written.
        """
        start = self._boundary_of(self._now()) if boundary is None else boundary
        running, queue = self._snapshot()
        window = {
            "start": _stamp(start),
            "end": _stamp(start + self._window_seconds),
            "running": running,
            "queue": queue,
        }
        reference = self._reference_window(start)
        if reference is None:
            # No comparison side: the window says so instead of
            # claiming an agreement nobody looked for.
            window["differences"] = []
            window[WINDOW_COMPARED_KEY] = False
            window["consistent"] = None
        else:
            differences = compare_windows(
                window,
                reference,
                admitted=self._admitted,
                stopped=self._stopped,
                workspaces=self._workspaces(queue),
            )
            window["differences"] = differences
            window[WINDOW_COMPARED_KEY] = True
            window["consistent"] = not differences
        self._windows.append(window)
        self._write_windows()
        self._write_diff()
        return window

    def flush(self) -> None:
        """Write the windows and the report, as they are right now."""
        self._write_windows()
        self._write_diff()

    # ------------------------------------------------------------------
    # The internals
    # ------------------------------------------------------------------

    def _as_record(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Return one decision as a tick line.

        A line is the four canonical fields and nothing else, so two
        runs over the same decisions write byte-identical lines: the
        line is meant to be read, and to be diffed against the other
        engine's line.
        """
        record = {field: decision.get(field) for field in RECORD_FIELDS}
        if record.get("ts") is None:
            record["ts"] = _stamp(self._now())
        return record

    def _remember(self, records: Sequence[Dict[str, Any]]) -> None:
        """Remember what the dry run decided, for the comparison to use.

        A plan the dry run admitted is one the old engine may not have
        started, and a plan the dry run stopped is one the old engine
        may still be running.  Which of the two it is cannot be read
        off a snapshot alone, so the decisions are kept and the
        comparison asks them.
        """
        for record in records:
            action = str(record.get("action") or "")
            plan_id = str(record.get("plan_id") or "")
            if not plan_id:
                continue
            if action == "admit":
                if plan_id not in self._admitted:
                    self._admitted.append(plan_id)
            elif action == "stop":
                if plan_id not in self._stopped:
                    self._stopped.append(plan_id)

    def _reference_window(self, start: float) -> Optional[Dict[str, Any]]:
        """The old engine's record for the window that starts at ``start``.

        Returns:
            The record, or ``None`` when the old engine has none — in
            which case the window is not compared.
        """
        if self._reference is None:
            return None
        return self._reference.window_at(_stamp(start))

    def _workspaces(self, queue: Sequence[Dict[str, Any]]) -> List[str]:
        """The order in which the queued plans' workspaces were served.

        The order is the queue's own order: the head of the line is
        served first.  A writer that does not know the workspaces
        cannot answer the workspace question, and answers nothing.
        """
        if self._workspace_of is None:
            return []
        order: List[str] = []
        for row in queue:
            try:
                key = self._workspace_of(str(row.get("plan_id") or ""))
            except Exception:  # noqa: BLE001 — a fact that cannot be read is no fact
                continue
            if key and str(key) not in order:
                order.append(str(key))
        return order

    def _append_ticks(self, records: List[Dict[str, Any]]) -> None:
        """Append the tick lines to the day's file."""
        self.day_dir.mkdir(parents=True, exist_ok=True)
        if self._prior_ticks is None:
            # The day's file may already hold the earlier runs'
            # decisions; the count is taken before this run's first line
            # is written, so the report counts the day's decisions and
            # not this run's.
            self._prior_ticks = self._count_lines(self.ticks_path)
        with open(self.ticks_path, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                )
        self._ticks += len(records)

    @staticmethod
    def _count_lines(path: Path) -> int:
        """Return how many lines the file holds."""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0

    def _catch_up(self) -> None:
        """Record every window boundary the clock has reached.

        The first moment the writer sees fixes the window the run
        starts in, and that window is recorded: a dry run that began
        at 10:29 has been running inside the 10:00 window, and the
        comparison has to see that window's state.

        Every boundary after that is recorded when the clock reaches
        it.  A clock that jumped over a boundary records the window it
        jumped over with the state it can see now — the state of the
        past is not kept, and inventing it would be worse than saying
        what is.
        """
        boundary = self._boundary_of(self._now())
        if self._last_boundary is None:
            self._last_boundary = boundary
            self.record_window(boundary)
            return
        while boundary > self._last_boundary:
            self._last_boundary += self._window_seconds
            self.record_window(self._last_boundary)

    def _boundary_of(self, moment: float) -> float:
        """Return the moment the window the moment falls in starts at."""
        return float(int(moment // self._window_seconds) * self._window_seconds)

    def _day(self) -> str:
        """Return the name of the day the clock is on."""
        return datetime.fromtimestamp(self._now(), tz=timezone.utc).strftime(
            "%Y%m%d"
        )

    def _now(self) -> float:
        """The current moment, in epoch seconds."""
        value = self._clock()
        return float(value) if value is not None else time.time()

    def _snapshot(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Return the running set and the queue, as the tables hold them.

        These are the scheduler's own facts, read through the store:
        the slots say which plans hold compute and the queue says in
        which order the waiting plans may run.  A window without them
        would say nothing about what the scheduler was doing.
        """
        if self._store is None or self._conn is None:
            return [], []

        running = [
            {
                "plan_id": str(row.get("plan_id") or ""),
                "acquired_at": row.get("acquired_at"),
            }
            for row in self._store.list_slots(self._conn)
        ]
        queue = [
            {
                "plan_id": str(row.get("plan_id") or ""),
                "seq": row.get("seq"),
                "priority": row.get("priority"),
            }
            for row in self._store.list_queue(self._conn)
        ]
        return running, queue

    def _merged_windows(self) -> List[Dict[str, Any]]:
        """The windows of this day: the earlier runs' and this run's.

        A window that is already on record keeps the record it has — a
        second run adds to the day's evidence, it does not replace it.
        """
        merged = self._existing_windows()
        seen = {str(window.get("start")) for window in merged}
        for window in self._windows:
            if str(window.get("start")) in seen:
                continue
            seen.add(str(window.get("start")))
            merged.append(window)
        return merged

    def _existing_windows(self) -> List[Dict[str, Any]]:
        """The windows the day's file already holds."""
        path = self.windows_path
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if isinstance(payload, list):
            return [window for window in payload if isinstance(window, dict)]
        if isinstance(payload, dict):
            for key in ("windows", "records", "entries"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [
                        window for window in value if isinstance(window, dict)
                    ]
        return []

    def _write_windows(self) -> None:
        """Write the day's windows.

        The day's windows are read *before* the file is opened for
        writing: opening it truncates it, and a day's evidence that is
        read after the truncation is a day's evidence that is gone.
        """
        windows = self._merged_windows()
        self.day_dir.mkdir(parents=True, exist_ok=True)
        with open(self.windows_path, "w", encoding="utf-8") as handle:
            json.dump(
                windows,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")

    def _write_diff(self) -> None:
        """Write the report over the day's windows.

        The report says what the run covered and what the judge makes
        of the windows it recorded.  It does not claim the two
        schedulers agree: that claim needs the old engine's windows,
        and until they are here the verdict is "not compared yet".
        """
        windows = self._merged_windows()
        verdict = evaluate_windows(windows)

        lines = [
            "# dry-run 对拍产物",
            "",
            "- 产物目录：{0}".format(self.day_dir),
            "- 窗口数：{0}".format(len(windows)),
            "- tick 决策数：{0}".format(self.day_ticks),
            "",
            "## 逐周期窗口",
            "",
            "| 窗口开始 | 窗口结束 | 运行集 | 队列 | 对照 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for window in windows:
            running = ", ".join(str(row.get("plan_id")) for row in window.get("running", []))
            queue = ", ".join(str(row.get("plan_id")) for row in window.get("queue", []))
            lines.append(
                "| {0} | {1} | {2} | {3} | {4} |".format(
                    window.get("start"),
                    window.get("end"),
                    running or "-",
                    queue or "-",
                    self._comparison_note(window),
                )
            )

        lines.extend(
            [
                "",
                "## 判定",
                "",
                "判定器 `plan_scheduler.dryrun.evaluate_windows` 对本文件记录的 "
                "{0} 个窗口的判定：".format(len(windows)),
                "",
                "- 一致：{0}".format(verdict.consistent),
                "- 仅含允许差异：{0}".format(verdict.allowed_only),
                "- 含不允许差异：{0}".format(verdict.forbidden),
                "- 未对照：{0}".format(verdict.uncompared),
                "",
                self._conclusion(windows, verdict),
                "",
            ]
        )

        self.day_dir.mkdir(parents=True, exist_ok=True)
        self.diff_path.write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _comparison_note(window: Mapping[str, Any]) -> str:
        """Say, in one word, what the comparison found in a window."""
        if window.get(WINDOW_COMPARED_KEY) is not True:
            return "未对照"
        differences = window.get("differences") or []
        if not differences:
            return "一致"
        return "、".join(
            str(difference.get("kind"))
            for difference in differences
            if isinstance(difference, Mapping)
        ) or "有差异"

    @staticmethod
    def _conclusion(
        windows: Sequence[Dict[str, Any]], verdict: Verdict
    ) -> str:
        """Return the report's conclusion.

        A window that was compared has a verdict, and the judge's
        verdict of all of them is the answer.  A window that was not
        compared has none: this writer holds one side of the
        comparison, and the old engine's windows are what the other
        side is.  A dry run with an uncompared window in it has not
        proved anything, and it says so.
        """
        if verdict.forbidden:
            return "结论：**不通过** —— 有 {0} 个周期含不允许差异。".format(
                verdict.forbidden
            )
        if verdict.uncompared:
            return (
                "结论：**尚未完成对拍** —— {0} 个周期没有对照侧（未对照），"
                "本轮不能进入切换门禁；对照侧由 "
                "`PDT_DRYRUN_REFERENCE` 指向的旧引擎记录提供。".format(
                    verdict.uncompared
                )
            )
        if verdict.passed:
            return "结论：**通过**"
        return (
            "结论：**不通过** —— {0} 个周期中只有 {1} 个完全一致，"
            "不足 {2} 个。".format(
                len(windows), verdict.consistent, MIN_CONSISTENT
            )
        )
