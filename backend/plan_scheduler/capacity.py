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

The framework-fix lane's own ceilings
----------------------------------
The scheduler's capacity is one number: how many plans may work at the
same time.  The framework-fix lane adds a second, narrower question —
*how many of those may be repairs of the framework itself*.  The
architecture pins three numbers for it: at most **two** active fix
plans overall, at most **two** per framework repository, and **one**
slot beyond the ceiling.

Those three numbers are ceilings, not counters, and they are set by the
operator at runtime: they live in ``scheduler_setting`` as
``framework_fix.max_active``, ``framework_fix.max_per_repo`` and
``framework_fix.oversubscribe``.  A setting that is a row survives a
restart, so an operator who turns a ceiling down finds it still turned
down after the scheduler comes back.

The lane's admission answers three things at once — may this plan
start, why not, and did it take the slot beyond the ceiling — because
the scheduler has to be able to say *which* of them ran out.  A repair
plan that is refused because its repository is full is not the same
situation as one that is refused because somebody else holds that
repository's merge slot, and the operator reads the difference.

The slot beyond the ceiling is a **lease**.  It is the one place where
the scheduler may run more plans than its capacity allows, and it
belongs to the lane: at most one repair plan holds it at a time, and
the holder is written down, so "who is on it now" has an answer.  A
lease somebody else holds is a wait, not a refusal; the plan that
holds it may ask again without being in its own way.  An ordinary plan
never holds this lease — the slot is the lane's own — so a machine that
ordinary plans pushed past its capacity does not shut the repair lane's
door.

The ceiling and the slot are two different numbers.  The lane may run
**two** repair plans at a time, and those two places are one ordinary
slot and one oversubscribed one: the slot beyond the capacity does not
raise the ceiling, it is *inside* it.

A slot that a preempted plan frees up is an ordinary slot.  The repair
plan that takes it is not on the slot beyond the capacity, so it does
not hold the lease — otherwise the lane would give its one slot beyond
the capacity to a plan that is nowhere near it, and the plan that
really needs it would wait.

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

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .config import DEFAULT_CAPACITY, DEFAULT_MIN_RUNTIME_SECONDS
from .policy import COUNTED_PHASES, FRAMEWORK_FIX_KIND

__all__ = [
    "CAPACITY_OVERRIDE_KEY",
    "DEFAULT_FRAMEWORK_FIX_MAX_ACTIVE",
    "DEFAULT_FRAMEWORK_FIX_MAX_PER_REPO",
    "DEFAULT_FRAMEWORK_FIX_OVERSUBSCRIBE",
    "FRAMEWORK_FIX_MAX_ACTIVE_KEY",
    "FRAMEWORK_FIX_MAX_PER_REPO_KEY",
    "FRAMEWORK_FIX_OVERSUBSCRIBE_KEY",
    "FRAMEWORK_FIX_OVERSUBSCRIBE_LEASE_KEY",
    "NORMAL_KIND",
    "CapacityLedger",
]


#: The key the runtime capacity override is stored under.
#:
#: The capacity lives in ``backend/config.yaml`` — a file that is
#: tracked by git and carries the comments that explain it, so it may
#: not be rewritten by a YAML dump.  An operator who wants the
#: scheduler to run six plans at a time instead of four needs that
#: number to change while the scheduler runs, and that number must not
#: rewrite a tracked file to get there.  So the override is a *row*:
#: the state database holds it, the configuration file stays as it is,
#: and removing the row is what puts the configured capacity back.
#:
#: The constant lives here, beside the ledger that reads it, rather than
#: in ``plan_scheduler.store``: the route that writes the override reaches
#: the row through the engine's own connection, and a route must not
#: import the module that owns the scheduler's tables (see
#: ``tests/regression/plan_scheduler/test_regression_workspace_and_single_writer.py``).
CAPACITY_OVERRIDE_KEY = "capacity_override"


# ---------------------------------------------------------------------------
# The framework-fix lane's ceilings
# ---------------------------------------------------------------------------

#: The settings key the **global** ceiling lives under: how many
#: framework-fix plans may hold compute at the same time, everywhere.
FRAMEWORK_FIX_MAX_ACTIVE_KEY = "framework_fix.max_active"

#: The settings key the **per-repository** ceiling lives under: how many
#: framework-fix plans may hold compute at the same time in one
#: framework repository.
FRAMEWORK_FIX_MAX_PER_REPO_KEY = "framework_fix.max_per_repo"

#: The settings key the count of slots **beyond** the ceiling lives
#: under: how many plans may be run past the ceiling.
FRAMEWORK_FIX_OVERSUBSCRIBE_KEY = "framework_fix.oversubscribe"

#: The settings key the **lease** on the slot beyond the ceiling lives
#: under.
#:
#: The slot is not a place two plans may sit in: it is a lease, and the
#: lease names the plan that holds it.  The row is what answers "who is
#: on the slot beyond the capacity now" — a question a count cannot
#: answer, because the count only says how many plans are running, not
#: which of them took the lane's own slot.
FRAMEWORK_FIX_OVERSUBSCRIBE_LEASE_KEY = "framework_fix.oversubscribe_lease"

#: The kind an ordinary plan carries: the word the routing table's
#: ``plan_kind`` column holds for a plan that is not a repair of the
#: framework itself.  It is the column's own default, repeated here so
#: that the ledger names the word it writes rather than repeating a
#: literal in two places.
NORMAL_KIND = "normal"

#: The ceilings the lane runs with when nobody has set them.
#:
#: These are the numbers the architecture pinned: at most two active fix
#: plans overall, at most two per framework repository, and one slot
#: beyond the ceiling.  ``backend.config``'s
#: ``DEFAULT_FRAMEWORK_FIX_CONFIG`` carries the same three numbers for
#: the configuration file; the two must agree, and the runtime setting
#: wins over both.
DEFAULT_FRAMEWORK_FIX_MAX_ACTIVE = 2
DEFAULT_FRAMEWORK_FIX_MAX_PER_REPO = 2
DEFAULT_FRAMEWORK_FIX_OVERSUBSCRIBE = 1


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


def _plan_ids(items: Any) -> List[str]:
    """The plan ids a collection of plans or of rows names.

    A plain id is taken as it is, and a row is read for its
    ``plan_id``.  A mapping is read by its **keys** — the ledger reads
    its slot rows as a mapping of plan id to row, and there the keys
    are the ids.
    """
    candidates: Iterable[Any]
    candidates = items.keys() if isinstance(items, Mapping) else items
    ids: List[str] = []
    for candidate in candidates:
        if isinstance(candidate, str):
            ids.append(candidate)
        else:
            ids.append(str(_field(candidate, "plan_id", "")))
    return ids


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


def _safe_priority(row: Any) -> int:
    """Return the priority the slot carries, or ``0`` when it carries none.

    A priority is a whole number.  A value that is missing, ``NULL``,
    a boolean, a string or a number that is not whole reads as ``0`` —
    the weight every plan carries when nothing says otherwise.

    The reading never lifts a plan to the front of the order by
    accident: a priority the ledger cannot read leaves the plan at the
    ordinary weight, and the runtime decides among the plans that
    share it.  A boolean is not a number here (``True`` is not ``1``),
    and a string is not a number either — the priority is written down
    as a number, and anything else is a value the ledger does not know
    how to compare.
    """
    value = _field(row, "priority", 0)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return 0


def _count_of(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Return ``value`` as a whole number, or ``default`` when it is not one.

    A count is a whole number, and a negative count is not a count.  A
    boolean is not a number either — ``True`` is not ``1`` as far as a
    ceiling is concerned — and a string that spells a number *is* one,
    because the settings table stores everything as text.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value >= 0 else default
    if isinstance(value, float) and value.is_integer():
        return int(value) if value >= 0 else default
    if isinstance(value, str):
        try:
            number = int(value.strip())
        except (TypeError, ValueError):
            return default
        return number if number >= 0 else default
    return default


def _repo_of_block(raw: Any) -> str:
    """Return the repository a plan's framework-fix block names.

    The block is the JSON the routing row carries: ``{"repo": ...,
    "fingerprint": ...}``.  A block that cannot be read names no
    repository, and a plan that names none cannot be counted against
    any repository's ceiling — it still counts against the global one.
    """
    if raw is None:
        return ""
    if isinstance(raw, dict):
        block: Any = raw
    else:
        try:
            block = json.loads(str(raw))
        except (TypeError, ValueError):
            return ""
    if not isinstance(block, dict):
        return ""
    repo = block.get("repo")
    if repo is None:
        return ""
    return str(repo).strip()


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

        The ledger **reads before it writes**.  The working plans and
        the slot rows are read first, without taking SQLite's write
        lock, and the write transaction is opened only when the two
        disagree.  A tick that has nothing to write — the steady state,
        which is every tick but the ones where a plan started or
        finished — therefore does not make the other writers of the
        database wait, and that wait is where ``database is locked``
        comes from.

        When there is something to write, the write is one transaction:
        the slots and the moment they were taken are one fact about the
        plans that are working, and a half-applied reconciliation is a
        scheduler that counts a plan it has no record of.  The two sides
        are read **again** inside that transaction, because the
        difference may have disappeared between the light read and the
        moment the write lock was taken — and the read inside the
        transaction is the one that counts.

        Returns:
            A summary of what the reconciliation did: ``used`` (how many
            plans hold compute), ``added`` (the plans whose slot row was
            written), ``removed`` (the plans whose slot row was deleted)
            and ``preserved`` (the plans whose slot row was already
            there and was left alone).  The three lists are sorted, so
            the summary is the same on every run.
        """
        conn = self._require_conn()

        # The light read: the working plans and the slot rows, taken
        # without the write lock.
        active = self._active_ids()
        slots = self._slot_rows(conn)
        lease = self._oversubscribe_lease_holder()
        stale_lease = (
            lease is not None and lease not in active and lease not in slots
        )

        if not self._writes_needed(active, slots) and not stale_lease:
            # The slots already say what the facts say.  There is
            # nothing to write, so the ledger does not take SQLite's
            # write lock: a tick like this must not make the other
            # writers of the database wait for it.
            return {
                "used": len(active),
                "added": [],
                "removed": [],
                "preserved": sorted(active),
            }

        stamp = _to_iso(self._now())

        added: List[str] = []
        removed: List[str] = []
        preserved: List[str] = []

        with self._store.transaction(conn):
            # The light read may be stale by the time the write lock
            # is taken: a plan may have started or finished in between.
            # The read inside the transaction is the one that decides
            # what is written.
            active = self._active_ids()
            slots = self._slot_rows(conn)
            lease = self._oversubscribe_lease_holder()

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

            # The lease on the slot beyond the capacity follows its
            # holder: a holder that holds neither compute nor a slot row
            # is gone, and the lease goes back to the pool.
            if (
                lease is not None
                and lease not in active
                and lease not in {pid for pid in slots if pid not in removed}
            ):
                self._store.release_oversubscribe_lease(conn, lease)

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
        plan was created (``None`` when that is unknown), how long the
        plan has been running and **the priority the slot holds** — the
        number the policy reads first, because the plan that stands
        last in the line is the one that goes.  A priority the ledger
        cannot read is ``0``.

        Each candidate also carries **the protection facts**: the
        ``plan_kind`` the plan runs under (``framework_fix`` for a
        repair plan, ``normal`` for an ordinary one) and the
        ``preemptable`` flag that says whether the plan may be
        interrupted at all — ``False`` for a repair plan, ``True`` for
        an ordinary one.  They are on the candidate because the policy
        reads them there: :func:`plan_scheduler.policy.is_protected`
        looks the two keys up on the entry it is given, and an entry
        that carries neither cannot answer "is this plan protected?".
        The answer it gives then is "no", and a repair plan that has
        been working for hours is interrupted like any other — which is
        the livelock the framework-fix lane is built to make impossible.
        The facts are produced **here**, by the ledger, because the
        ledger is the one that knows what the plan is: the routing
        view (:meth:`_framework_fix_plans`) is the ledger's own.

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
            ``plan_id``, ``acquired_at``, ``created_at``, ``elapsed``,
            ``priority``, ``plan_kind`` and ``preemptable``.  Empty when
            there is nobody to interrupt, which is an answer and not an
            error.
        """
        moment = _to_epoch(now)
        if moment is None:
            return []
        threshold = _threshold(min_runtime_seconds)

        conn = self._require_conn()
        active = self._active_ids()
        starts = self._runtime_starts(conn)
        # The routing view, read once: it says which of the plans are
        # repair plans, and the candidate row has to say so too.
        fix_plans = self._framework_fix_plans()

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
                is_fix = plan_id in fix_plans
                candidates.append(
                    (
                        started,
                        plan_id,
                        {
                            "plan_id": plan_id,
                            "acquired_at": acquired,
                            "created_at": self._facts.plan_created_at(plan_id),
                            "elapsed": elapsed,
                            "priority": _safe_priority(slot),
                            "plan_kind": (
                                FRAMEWORK_FIX_KIND if is_fix else NORMAL_KIND
                            ),
                            "preemptable": not is_fix,
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
    # The framework-fix lane's admission
    # ------------------------------------------------------------------

    def framework_fix_limits(self) -> Dict[str, int]:
        """The lane's three ceilings, as they stand right now.

        The runtime setting wins over the built-in default: an operator
        who turns a ceiling down must have it honoured while the
        scheduler runs, and the row survives a restart because it is a
        row.  A row that does not hold a usable number is **ignored**
        rather than obeyed — the same rule the capacity override
        follows: a number the ledger cannot read is not a ceiling, and
        obeying it would be worse than the default.

        Returns:
            ``max_active`` (the global ceiling), ``max_per_repo`` (the
            per-repository ceiling) and ``oversubscribe`` (how many
            slots may be taken beyond the ceiling).
        """
        limits = {
            "max_active": DEFAULT_FRAMEWORK_FIX_MAX_ACTIVE,
            "max_per_repo": DEFAULT_FRAMEWORK_FIX_MAX_PER_REPO,
            "oversubscribe": DEFAULT_FRAMEWORK_FIX_OVERSUBSCRIBE,
        }
        conn = self._conn
        if conn is None:
            return limits
        for name, key in (
            ("max_active", FRAMEWORK_FIX_MAX_ACTIVE_KEY),
            ("max_per_repo", FRAMEWORK_FIX_MAX_PER_REPO_KEY),
            ("oversubscribe", FRAMEWORK_FIX_OVERSUBSCRIBE_KEY),
        ):
            value = self._setting_count(conn, key)
            if value is not None:
                limits[name] = value
        return limits

    def admit(
        self,
        *,
        kind: str,
        repo: Optional[str] = None,
        running_global: int = 0,
        plan_id: Optional[str] = None,
        capacity: Optional[int] = None,
        used: Optional[int] = None,
        freed_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """May this plan start, and if not, why not?

        The framework-fix lane has a narrower admission than an ordinary
        plan: a repair plan may take a slot beyond the scheduler's
        capacity (that is the *oversubscribe* slot), but only one, and
        only while its repository has room.

        The rules, in the order they are applied:

        1. **The merge slot.**  One repository has one un-merged repair
           PR at a time, and the lease on it lives on the framework-fix
           registry.  When somebody else holds the lease of *this*
           repository's slot, the plan waits for it — the reason word
           is ``waiting_merge_slot``.  The lease of another repository
           says nothing about this one.
        2. **The per-repository ceiling.**  At most
           ``max_per_repo`` repair plans of one repository hold compute
           at the same time.  One more than that is refused, and the
           reason word is ``blocked_by_capacity``.
        3. **The global ceiling.**  At most ``max_active`` repair plans
           hold compute at the same time, everywhere.  The slot beyond
           the capacity does **not** raise this number: the lane's two
           places are one ordinary slot and one oversubscribed one, and
           both of them are inside the same budget.  A plan that would
           be the third is refused with ``blocked_by_capacity``.
        4. **The slot.**  Inside that budget the plan needs a place to
           run.  A free ordinary slot is taken as it is — including the
           slot a plan the caller is pushing aside frees up.  When
           there is none, the plan may take the **oversubscribe slot** —
           the lane's own slot beyond the scheduler's capacity — and
           that slot is a **lease**: the holder is written down, so at
           most one repair plan holds it at a time.  Somebody else's
           lease is a wait (``blocked_by_capacity``); this plan's own
           lease is not a conflict, so the plan may ask again without
           being in its own way.

        A plan that is not a framework fix is not subject to any of
        this: the lane's ceilings are the lane's own.  For an ordinary
        plan the ledger says whether a slot is free, and nothing more.

        Args:
            kind: the plan's kind — ``framework_fix`` for a repair plan,
                anything else for an ordinary one.
            repo: the framework repository the plan repairs.  A plan
                that names none is bound by the global ceiling only.
            running_global: how many framework-fix plans are running
                globally, as the caller knows it.  The larger of this
                number and the ledger's own count is used: both are
                lower bounds on what is running, and under-counting
                would admit a plan past the ceiling.
            plan_id: the plan's own id, when the caller knows it.  It is
                what tells the lease holder apart from the holder
                itself — a plan that does not say who it is cannot tell
                its own lease from somebody else's, so it waits.
            capacity: how many plans the scheduler may run at the same
                time.  The runtime override is used when this is
                omitted, and the configured default when there is no
                override either.
            used: how many plans hold compute, as the caller counts
                them.  The caller knows about the plans it started in
                this very tick; the ledger knows about the ones the
                database says are working.  The larger of the two is
                used, because under-counting would hand out a slot that
                is already taken.
            freed_by: the plan the caller is pushing aside right now.
                Its slot frees up for the plan that is asking, so it is
                counted by neither side: the ledger's own count does not
                include it, and the caller's count has left it out
                already.

        Returns:
            ``{"admit": bool, "reason": str, "oversubscribed": int}``.
            The reason is always a word of the scheduler's closed
            vocabulary (``plan_scheduler.policy.REASONS``), and
            ``oversubscribed`` is ``1`` only when the plan takes the
            slot beyond the capacity.
        """
        if str(kind) != FRAMEWORK_FIX_KIND:
            return self._admit_ordinary(capacity)

        limits = self.framework_fix_limits()
        repository = str(repo).strip() if repo is not None else ""

        # 1. The merge slot of this repository.
        holder = self._merge_lease_holder(repository)
        if holder is not None and holder != str(plan_id or ""):
            return {
                "admit": False,
                "reason": "waiting_merge_slot",
                "oversubscribed": 0,
            }

        # 2. The per-repository ceiling.
        if repository:
            same_repo = self._framework_fix_count(repo=repository)
            if same_repo + 1 > limits["max_per_repo"]:
                return {
                    "admit": False,
                    "reason": "blocked_by_capacity",
                    "oversubscribed": 0,
                }

        # 3. The global ceiling.  The slot beyond the capacity does not
        #    raise it: the lane's two places are one ordinary slot and
        #    one oversubscribed one, and both are inside this budget.
        running = max(
            _count_of(running_global, 0), self._framework_fix_count()
        )
        if running + 1 > limits["max_active"]:
            return {
                "admit": False,
                "reason": "blocked_by_capacity",
                "oversubscribed": 0,
            }

        # 4. The slot.  A free ordinary slot is taken as it is; when
        #    there is none, the plan may take the lane's own slot beyond
        #    the capacity — the oversubscribe lease.
        if self._has_free_slot(capacity, used, freed_by):
            return {
                "admit": True,
                "reason": "framework_fix_priority",
                "oversubscribed": 0,
            }
        return self._admit_oversubscribed(limits, plan_id)

    def _admit_oversubscribed(
        self,
        limits: Mapping[str, int],
        plan_id: Optional[str],
    ) -> Dict[str, Any]:
        """The slot beyond the capacity — the lane's own lease.

        The lease is what makes the slot single-holder: the holder is
        written down, so "who is on it now" has an answer and two
        repair plans cannot both be on it.

        A lease that cannot be written down is a lease nobody holds,
        and a slot nobody holds is a slot two plans may take — so the
        plan waits instead.
        """
        if limits["oversubscribe"] < 1:
            # The lane may not run past the scheduler's capacity.
            return {
                "admit": False,
                "reason": "blocked_by_capacity",
                "oversubscribed": 0,
            }
        mine = str(plan_id or "").strip()
        if not mine:
            # Nobody can be written down as the holder, so the slot
            # cannot be claimed.
            return {
                "admit": False,
                "reason": "blocked_by_capacity",
                "oversubscribed": 0,
            }
        holder = self._oversubscribe_lease_holder()
        if holder is not None and holder != mine:
            # Somebody else holds the lease: the plan waits for it.
            return {
                "admit": False,
                "reason": "blocked_by_capacity",
                "oversubscribed": 0,
            }
        if not self._take_oversubscribe_lease(mine):
            return {
                "admit": False,
                "reason": "blocked_by_capacity",
                "oversubscribed": 0,
            }
        return {
            "admit": True,
            "reason": "framework_fix_priority",
            "oversubscribed": 1,
        }

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

    def _slot_rows(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """The slot rows that are there, by plan id.

        The rows are read in the order the store hands them over —
        oldest acquisition first — so that the ledger writes in the
        same order it always did.
        """
        return {
            str(_field(slot, "plan_id", "")): slot
            for slot in self._store.list_slots(conn)
        }

    def _writes_needed(self, active: Any, slots: Any) -> bool:
        """Whether the slot rows and the working plans disagree.

        ``active`` are the plans that are working and ``slots`` are the
        slot rows that are there — the mapping the ledger read, whose
        keys are the plan ids; a plain collection of ids or of rows is
        understood as well.  The two disagree when one of them names a
        plan the other does not: a plan that works without a slot row,
        or a slot row for a plan that does not work.  Either way there
        is something to write, and only then is the write lock worth
        taking.
        """
        return set(_plan_ids(active)) != set(_plan_ids(slots))

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

    def _admit_ordinary(self, capacity: Optional[int]) -> Dict[str, Any]:
        """The answer for a plan that is not a framework fix.

        The lane's ceilings are the lane's own — an ordinary plan is
        admitted by the scheduler's capacity, and the only thing this
        gate has to say is whether a slot is free.
        """
        if self.used() < self._capacity(capacity):
            return {"admit": True, "reason": "running", "oversubscribed": 0}
        return {"admit": False, "reason": "waiting_for_slot", "oversubscribed": 0}

    def _framework_fix_plans(self) -> Dict[str, str]:
        """The active framework-fix plans, and the repository each repairs.

        A plan counts when it holds compute (the ledger's unit) **and**
        its phase is one of :data:`plan_scheduler.policy.COUNTED_PHASES`:
        a plan that is queued, parked or waiting for a human holds
        nothing and counts for nothing.  The repository is the one its
        framework-fix block names — the plan's own record of what it
        repairs.

        A database that does not carry the routing table's ``plan_kind``
        column cannot answer, and a database that never ran the
        framework-fix migration has no repair plans in it either: the
        answer is then "none", and the lane is not running anyway.
        """
        active = set(self._active_ids())
        conn = self._conn
        if not active or conn is None:
            return {}
        try:
            rows = conn.execute(
                "SELECT plan_id, current_phase, plan_kind, framework_fix "
                "FROM plan_routing"
            ).fetchall()
        except sqlite3.Error:
            return {}

        plans: Dict[str, str] = {}
        for row in rows:
            plan_id = str(row[0])
            if plan_id not in active:
                continue
            if row[1] not in COUNTED_PHASES:
                continue
            if str(row[2] or "") != FRAMEWORK_FIX_KIND:
                continue
            plans[plan_id] = _repo_of_block(row[3])
        return plans

    def _framework_fix_count(self, repo: Optional[str] = None) -> int:
        """How many framework-fix plans hold compute.

        Args:
            repo: when given, only the plans of **that** repository are
                counted.
        """
        plans = self._framework_fix_plans()
        if repo is None:
            return len(plans)
        wanted = str(repo).strip()
        return sum(1 for value in plans.values() if value == wanted)

    def _merge_lease_holder(self, repo: str) -> Optional[str]:
        """Who holds this repository's merge slot; ``None`` when it is free.

        The lease is the framework-fix registry's own table — the
        architecture keeps the merge serialisation **there** and not in
        the capacity ledger.  The ledger only asks who holds it, so it
        can say that this plan has to wait for the slot.
        """
        conn = self._conn
        if conn is None or not repo:
            return None
        try:
            row = conn.execute(
                "SELECT fix_plan_id FROM framework_fix_merge_lease WHERE repo = ?",
                (repo,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        holder = row[0]
        return str(holder) if holder else None

    def _has_free_slot(
        self,
        capacity: Optional[int],
        used: Optional[int] = None,
        freed_by: Optional[str] = None,
    ) -> bool:
        """Whether an ordinary slot is free.

        The count is the larger of the caller's number and the ledger's
        own: the caller knows about the plans it started in this very
        tick, and the ledger knows about the ones the database says are
        working.  The larger of the two is the safe one — under-counting
        would hand out a slot that is already taken.

        A plan that is on its way out is counted by neither: the plan
        the caller is pushing aside (``freed_by``) is still working as
        far as the database knows, but its slot frees up for the plan
        that is asking — and the caller's count has left it out
        already.
        """
        by_db = self.used()
        vacancy = str(freed_by or "").strip()
        if vacancy and vacancy in set(self._active_ids()):
            by_db -= 1
        occupied = max(_count_of(used, 0) or 0, by_db)
        return occupied < self._capacity(capacity)

    def _oversubscribe_lease_holder(self) -> Optional[str]:
        """Who holds the slot beyond the capacity; ``None`` when it is free.

        The lease is a row in ``scheduler_setting`` — the scheduler's own
        table for the knobs that are set at runtime — so the answer
        survives a restart and is readable by anybody who asks the store.
        """
        conn = self._conn
        if conn is None:
            return None
        try:
            raw = self._store.get_oversubscribe_lease(conn)
        except Exception:  # noqa: BLE001 — a tick must not die here
            return None
        holder = str(raw or "").strip()
        return holder or None

    def _take_oversubscribe_lease(self, plan_id: str) -> bool:
        """Write this plan down as the holder of the slot beyond the capacity.

        A lease that cannot be written down is a lease nobody holds: the
        plan may not take the slot, because two plans could take it.
        """
        conn = self._conn
        if conn is None:
            return False
        try:
            self._store.set_oversubscribe_lease(conn, plan_id)
        except Exception:  # noqa: BLE001 — a tick must not die here
            return False
        return True

    def _capacity(self, capacity: Optional[int]) -> int:
        """How many plans the scheduler may run at the same time.

        The caller's number wins; otherwise the runtime override the
        operator set; otherwise the configured default.  The override is
        read from the same row the engine reads, so the two never
        disagree about how many plans may run.
        """
        given = _count_of(capacity)
        if given is not None:
            return given
        override = self._setting_count(self._conn, CAPACITY_OVERRIDE_KEY)
        if override is not None:
            return override
        return DEFAULT_CAPACITY

    def _setting_count(self, conn: Optional[sqlite3.Connection], key: str) -> Optional[int]:
        """Read one runtime setting as a whole number.

        ``None`` means "there is no usable value here": the setting was
        never written, or what was written is not a number the ledger
        can obey.  A setting the ledger cannot read is ignored rather
        than obeyed — the same rule the capacity override follows.
        """
        if conn is None:
            return None
        try:
            raw = self._store.get_setting(conn, key)
        except Exception:  # noqa: BLE001 — a tick must not die here
            return None
        if raw is None:
            return None
        return _count_of(raw)

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