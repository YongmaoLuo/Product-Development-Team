"""backend.plan_scheduler.capacity — the concurrency ledger.

Background
----------
The scheduler promises the operator something narrow: **at most ``N``
plans are working at the same time**, where ``N`` is the configured
capacity and the unit is the *plan* — not the subprocess, not the
verification thread.  Four plans verifying while four more plans
execute is eight plans working, and no amount of subprocess counting
would say otherwise.

Keeping that promise means answering two questions on every tick, and
they come from two different places.

**Who is working** is a *plan* fact.  A plan holds compute when its
execution subprocess is alive, or when its verification round is
running; both are derived from the plan tables by
:class:`plan_scheduler.facts.PlanFactsReader`, and the scheduler keeps
no copy of them.  That is the whole point: a copy would be a second
answer to the same question, and the two answers would drift.

**How long the plan has been running** is a *plan* fact, and the only
thing that needs it is the eviction rule — "a plan that has been
running for at least ``M`` seconds may be interrupted".  That moment is
the plan's own: ``plan_execution.started_at``, the moment the plan's
work began.  The slot row says when the plan took its slot, and that
timestamp is re-stamped whenever the engine adopts the plan, so it
cannot say how long the work has been going on.

The ledger is where the two are put back together.  Its reconciliation
is the authority: every tick it makes ``scheduler_slot`` say what the
plan facts say, so a slot row is never a second opinion about who is
running.

The reconciliation
------------------
A working plan with no slot row gets one.  That happens when the
operator started the plan through the lifecycle API, or when the
scheduler was restarted while the plan ran — the plan is working and
nobody wrote down when it started.  The row is stamped with **the
moment the plan was observed to be working**: for the execution side
that is the admission path, which writes the row when it starts the
plan, so a row that is missing here means this is the first observation;
for the verification side there is no admission path at all, and the
rule is stated as "now" — the moment the round was first seen.  The two
rules coincide here, because the ledger keeps no memory of an earlier
observation: it is this moment either way.  A plan that was already
running when the scheduler came back is therefore treated as if it had
just taken its slot, which is what the restart path does when it adopts
a running plan.

A slot row for a plan that is **not** working is deleted.  The plan
finished, or was stopped, or was never started, and the row now says
something untrue.  A ledger that only ever added rows would keep
counting plans that finished long ago and would report a full capacity
while nothing was running.

The timer is never refreshed
----------------------------
Writing a slot row that is already there does not restart its clock,
and neither does a plan changing phase.  A plan walks through the
verification phases — ``verify_first_pass``, ``verify_recheck``,
``verification_repairing``, ``verification_rerunning`` — and it is the
same plan, holding the same slot, for the whole walk.  A timer that
restarted on every phase change would let a plan that had been running
for an hour look brand new, and the eviction rule would be satisfied by
a plan that had not finished anything.

Where the runtime is measured from
----------------------------------
The eviction rule asks how long a plan has been running, and the answer
is the moment the plan's own work began — ``plan_execution``'s
``started_at``.  The slot's timestamp cannot answer it: the engine
re-stamps the slots of the plans it adopts, so after a restart every
plan looks as if it had just taken its slot, and for the next ``M``
seconds nobody may be interrupted at all — the operator asks for a plan
to start and is told ``no_eligible_victim`` while the slot sits full of
work that has been going on for hours.

A plan whose own start is unknown — a row that says nothing, or a
timestamp that cannot be read — is judged by its slot instead, which is
the rule the ledger always had.  Each candidate carries the plan's
creation time as well, so that the policy can tell one plan from
another; a plan the facts cannot date carries ``None``, and the policy
falls back to the slot timestamp for it.

The workspace
-------------
Two plans may not work in the same directory at the same time.  The
rule is about *compute*, so it looks at the plans that are working and
not at the plans that are waiting: a plan in the queue is not working,
and it does not make its directory busy for the plan behind it.  The
directory is compared by its real path, so the same directory under
three different names — itself, a symlink to it, a symlink to that
symlink — is one directory, and the rule survives a restart, when
there is nothing in memory to compare.

The clock is a parameter
------------------------
Every time this module reads is read through an injected clock, because
the numbers it works with are large: the eviction threshold is 600
seconds and the verification staleness rule is 15 minutes.  A case that
had to wait for either of them would take longer than the thing it
measures, so the cases inject a clock they move by hand and the
production code injects :func:`time.time`.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import DEFAULT_MIN_RUNTIME_SECONDS

__all__ = ["CapacityLedger"]


# ---------------------------------------------------------------------------
# Reading the values
# ---------------------------------------------------------------------------


def _field(row: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` out of ``row``, falling back to ``default``.

    A row is read the same way whatever shape it has: a mapping answers
    ``get``, anything else answers ``getattr``.  A value that is present
    but ``NULL`` reads as the default.
    """
    getter = getattr(row, "get", None)
    if callable(getter):
        value = getter(name, default)
    else:
        value = getattr(row, name, default)
    return default if value is None else value


def _to_epoch(value: Any) -> Optional[float]:
    """Return ``value`` as epoch seconds, or ``None`` when it is not a time.

    Three shapes are understood — epoch seconds, a ``datetime`` and an
    ISO-8601 string — because the ledger is handed a clock in one of
    them and reads timestamps in another.  ``None`` means "this is not a
    time I can read", and a moment that cannot be read cannot be
    compared: the plan is then treated as young, which protects it
    rather than evicting it.
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


def _started_epoch(value: Any) -> Optional[float]:
    """Return the moment a plan's work began, in epoch seconds.

    ``plan_execution.started_at`` is written by the execution path as a
    **naive local** ISO-8601 stamp — ``datetime.now().isoformat()`` — so
    a stamp that names no zone is read as the local time it names.  A
    stamp that carries a zone, and a plain number of epoch seconds, are
    read as they say.

    The distinction is the whole rule.  Reading a local stamp as UTC
    would move the start of every plan into the future by the offset
    between the two, and a plan that has been working for an hour would
    look as if it had not started at all.

    ``None`` means "this is not a time I can read", and the caller falls
    back to the slot's timestamp — the moment the plan took its slot,
    which is what the ledger always measured from.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.astimezone()
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
            # A stamp with no zone is a local stamp.
            parsed = parsed.astimezone()
        return parsed.timestamp()
    return None


def _to_iso(epoch: float) -> str:
    """Return the moment as the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _workspace_key_of(value: Any) -> Optional[str]:
    """Return the one key a directory is known by.

    The key is the *real* path — ``realpath`` and then ``normpath`` — so
    the directory, a symlink to it and a symlink to that symlink are one
    key.  It is the same rule
    :meth:`plan_scheduler.facts.PlanFactsReader.workspace_key` applies
    when it derives the key, so the two halves of the comparison agree.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return os.path.normpath(os.path.realpath(text))


def _connection_of(facts: Any) -> Optional[sqlite3.Connection]:
    """Return the connection the facts reader reads on.

    The ledger writes the slots and the reader derives the plans, and
    both have to see the same database: a ledger that wrote to another
    file would be reconciling a world of its own.  The reader holds the
    connection it was given, so that is where the ledger takes its own
    from.
    """
    for name in ("conn", "_conn"):
        conn = getattr(facts, name, None)
        if isinstance(conn, sqlite3.Connection):
            return conn
    return None


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


class CapacityLedger:
    """The concurrency slots, reconciled against the plan facts.

    The ledger holds no state.  Every answer it gives is derived from
    the rows that are in the database at that moment: the working plans
    from the facts reader, the slots from the scheduler store.  A
    restarted ledger therefore says exactly what the ledger before it
    said — there is nothing to lose.

    Attributes:
        store: the scheduler's own store — the only writer of
            ``scheduler_slot``.
        facts: the reader that derives the working plans from the plan
            tables.
        clock: the source of "now", in epoch seconds.  It is a parameter
            so that the eviction threshold can be exercised without
            waiting ten minutes for it.
    """

    def __init__(
        self,
        store: Any,
        facts: Any,
        clock: Any = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Bind the ledger to a store, to the facts and to a clock."""
        self._store = store
        self._facts = facts
        self._clock = clock if clock is not None else time.time
        self._conn = conn if conn is not None else _connection_of(facts)

    # ------------------------------------------------------------------
    # The reconciliation
    # ------------------------------------------------------------------

    def reconcile(self) -> Dict[str, Any]:
        """Make ``scheduler_slot`` say what the plan facts say.

        A working plan with no slot row gets one, stamped with the
        moment it was observed to be working; a slot row for a plan that
        is not working is deleted.  A row that is already there is left
        alone — including its ``acquired_at``, because a plan that
        changes phase is still the same plan holding the same slot.

        The whole reconciliation is one transaction: the slots and the
        moment they were taken are one fact about the plans that are
        working, and a half-applied reconciliation is a scheduler that
        counts a plan it has no record of.

        Returns:
            A summary of what the reconciliation did: ``used`` (how many
            plans hold compute), ``added`` (the plans whose slot row was
            written), ``removed`` (the plans whose slot row was deleted)
            and ``preserved`` (the plans whose slot row was already
            there and was left alone).  The three lists are sorted, so
            the summary is the same on every run.
        """
        conn = self._require_conn()
        stamp = _to_iso(self._now())

        added: List[str] = []
        removed: List[str] = []
        preserved: List[str] = []

        with self._store.transaction(conn):
            active = self._active_ids()
            slots = {
                str(_field(slot, "plan_id", "")): slot
                for slot in self._store.list_slots(conn)
            }

            for plan_id in slots:
                if plan_id not in active:
                    self._store.delete_slot(conn, plan_id)
                    removed.append(plan_id)

            for plan_id in active:
                if plan_id in slots:
                    # The row is already there, and its clock is not
                    # restarted: a plan that changes phase is still the
                    # same plan holding the same slot.
                    preserved.append(plan_id)
                    continue
                self._store.upsert_slot(conn, plan_id, stamp)
                added.append(plan_id)

        return {
            "used": len(active),
            "added": sorted(added),
            "removed": sorted(removed),
            "preserved": sorted(preserved),
        }

    # ------------------------------------------------------------------
    # The used count
    # ------------------------------------------------------------------

    def used(self) -> int:
        """Return how many plans hold compute.

        The unit is the plan: the execution side and the verification
        side unioned, and a plan that is on both sides counted once —
        it holds one slot, not two.  The number comes from the plan
        facts, because "who is working" is a plan fact; ``scheduler_slot``
        is the record of *when* each of those plans took its slot, not
        the count itself.

        Returns:
            The number of plans holding compute.  Zero when nothing is
            working.
        """
        return len(self._active_ids())

    # ------------------------------------------------------------------
    # The eviction candidates
    # ------------------------------------------------------------------

    def evictable(
        self,
        now: Any,
        min_runtime_seconds: float = DEFAULT_MIN_RUNTIME_SECONDS,
    ) -> List[Dict[str, Any]]:
        """Return the plans that may be interrupted.

        A plan is a candidate when it is **working** and has been
        running for at least ``min_runtime_seconds``.  The threshold is
        ``>=`` and not ``>``: the default protection of 600 seconds is
        exactly that long, and a rule that is off by one either voids
        the protection or stalls the scheduler for good.

        **The runtime is measured from the plan's own start** — the
        moment the plan's work began, which is ``plan_execution``'s
        ``started_at``.  The slot's own timestamp cannot answer it: the
        engine re-stamps the slots of the plans it adopts, so after a
        restart every plan looks as if it had just taken its slot, and
        for the next ``M`` seconds nobody may be interrupted at all.  A
        plan whose own start is unknown is judged by its slot instead,
        which is the rule the ledger always had.

        Each candidate carries what the policy needs to choose among
        them: the plan id, the moment the slot was taken, the moment the
        plan was created (``None`` when that is unknown) and how long
        the plan has been running.

        The order is the ledger's own — the runtime start first, ties
        broken by plan id — so two calls answer in the same order and a
        caller that walks the list is never at the mercy of how SQLite
        returned the rows.

        Args:
            now: the moment to measure the runtime against.
            min_runtime_seconds: the runtime a plan is protected for —
                ``M``.

        Returns:
            The plans that may be interrupted, each a dict of
            ``plan_id``, ``acquired_at``, ``created_at`` and ``elapsed``.
            Empty when there is nobody to interrupt, which is an answer
            and not an error.
        """
        moment = _to_epoch(now)
        if moment is None:
            return []
        threshold = _threshold(min_runtime_seconds)

        conn = self._require_conn()
        active = self._active_ids()
        starts = self._runtime_starts(conn)

        candidates: List[Tuple[float, str, Dict[str, Any]]] = []
        for slot in self._store.list_slots(conn):
            plan_id = str(_field(slot, "plan_id", ""))
            if plan_id not in active:
                # A row left over from a plan that has since stopped:
                # interrupting it would interrupt nothing.
                continue
            acquired = _to_epoch(_field(slot, "acquired_at"))
            if acquired is None:
                continue
            started = starts.get(plan_id)
            if started is None:
                # The plan's own start is unknown: the slot's timestamp
                # is the answer the ledger always had.
                started = acquired
            elapsed = moment - started
            if elapsed >= threshold:
                candidates.append(
                    (
                        started,
                        plan_id,
                        {
                            "plan_id": plan_id,
                            "acquired_at": acquired,
                            "created_at": self._facts.plan_created_at(plan_id),
                            "elapsed": elapsed,
                        },
                    )
                )

        candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
        return [entry for _, _, entry in candidates]

    # ------------------------------------------------------------------
    # The workspace
    # ------------------------------------------------------------------

    def workspace_busy(self, key: Any) -> bool:
        """Return whether a directory is taken by a working plan.

        The rule is about compute, not about order: a plan in the queue
        is not working, so it does not make its directory busy for the
        plan behind it.  The directory is compared by its real path, so
        the same directory under three different names is one directory
        and one busy answer.

        Args:
            key: the workspace key to ask about.

        Returns:
            ``True`` when a working plan holds that directory.
        """
        if key is None:
            return False
        raw = str(key).strip()
        if not raw:
            return False
        target = _workspace_key_of(raw)

        for row in self._active_rows():
            active_key = _field(row, "workspace")
            if active_key is None:
                continue
            active_raw = str(active_key).strip()
            if not active_raw:
                continue
            if active_raw == raw:
                return True
            if target is not None and _workspace_key_of(active_raw) == target:
                return True
        return False

    # ------------------------------------------------------------------
    # The internals
    # ------------------------------------------------------------------

    def _active_rows(self) -> List[Dict[str, Any]]:
        """The plans that are working, as the facts derive them."""
        rows = self._facts.active_plans()
        return [row for row in rows if _field(row, "plan_id")]

    def _active_ids(self) -> List[str]:
        """The ids of the plans that are working, each of them once."""
        seen: List[str] = []
        for row in self._active_rows():
            plan_id = str(_field(row, "plan_id", ""))
            if plan_id and plan_id not in seen:
                seen.append(plan_id)
        return seen

    def _runtime_starts(self, conn: sqlite3.Connection) -> Dict[str, float]:
        """Return the moment each plan's own work began.

        ``plan_execution.started_at`` is the moment the plan's work
        began, and it is the one timestamp in this story that no
        reconciliation rewrites: the engine re-stamps the slots of the
        plans it adopts, but it never touches the plan's own start.

        A database that does not carry the execution table cannot
        answer the question, and the slot's timestamp is the answer the
        ledger always had — so the ledger falls back to it rather than
        failing.

        Args:
            conn: the connection to read on.

        Returns:
            The start of every plan that has one, by plan id.
        """
        try:
            rows = conn.execute(
                "SELECT plan_id, started_at FROM plan_execution"
            ).fetchall()
        except sqlite3.Error:
            return {}

        starts: Dict[str, float] = {}
        for row in rows:
            plan_id = row[0]
            if plan_id is None:
                continue
            started = _started_epoch(row[1])
            if started is None:
                continue
            starts[str(plan_id)] = started
        return starts

    def _now(self) -> float:
        """The current moment, in epoch seconds."""
        value = self._clock() if callable(self._clock) else self._clock
        moment = _to_epoch(value)
        return time.time() if moment is None else moment

    def _require_conn(self) -> sqlite3.Connection:
        """The connection to write the slots on."""
        if self._conn is None:
            raise RuntimeError(
                "the capacity ledger has no connection to write the slots "
                "on: the facts reader it was given does not expose one"
            )
        return self._conn


def _threshold(min_runtime_seconds: Any) -> float:
    """Return the runtime a plan is protected for, as a number.

    A threshold the ledger cannot read is a threshold it cannot compare.
    The configured default is used instead, which protects the plan
    rather than evicting it.
    """
    if isinstance(min_runtime_seconds, bool):
        return DEFAULT_MIN_RUNTIME_SECONDS
    try:
        return float(min_runtime_seconds)
    except (TypeError, ValueError):
        return DEFAULT_MIN_RUNTIME_SECONDS