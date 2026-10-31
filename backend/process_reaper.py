"""Reap a plan's process tree from the ledger — child first, by name.

Why this file exists
--------------------
PDT's process tree has four layers and **every one of them detaches
from its parent**: the executor runs in its own session, a sub-agent in
another, a VP service in yet another. Each detachment is deliberate — a
backend restart must not kill the executor, a sub-agent must survive its
own tools, a VP service has to outlive the backend that started it — and
each one costs the tree the ability to propagate a kill downwards.

So ``_kill_process_tree``, which signals the executor's own process
group, reaches exactly one layer: the executor. Everything detached
above it keeps running, holding ports and burning tokens, with nobody
left to own it. After a backend restart it is worse: the in-memory
handle is gone too, so *nothing* knows which pids belonged to the plan.

The ledger (task 1) is what knows. This module is what acts on it.

Three rules, and they are the whole design
------------------------------------------
**Child before parent.**  A sub-agent holds the tools it started; a VP
service holds the port. Killing the parent first leaves exactly the
processes the sweep is trying to collect running with no owner. So the
roles are reaped in a fixed order — :data:`REAP_ORDER` — and the children
(:data:`CHILD_ROLES`) go first.

**Identity before signal.**  Pids are recycled. "The ledger says 4242"
is not "4242 is ours"; the kernel's start time and command line are.
Every signal is preceded by that check, and a mismatch is *reported and
skipped* — the reaper writes an ``identity_mismatch`` event and sends
nothing at all. A pid it cannot vouch for is not a pid it may kill.

The check is repeated before *every* signal, not once per process.
The grace window between SIGTERM and SIGKILL is long enough for a pid
to be handed to somebody else, so the escalation re-reads the kernel
instead of trusting the reading it took before the SIGTERM.

The check vouches for a *pid*, and a signal is often addressed to a
*group* — which reaches every member of it.  So a group is only
addressed as a whole when every one of its members is vouch'd for as
well: each member is looked up in the plan's ledger and checked
against it the same way.  A group that holds a member the ledger does
not vouch for is not signalled as a group — the vouch'd process is
signalled on its own, and the member that is not vouch'd for keeps
running.  A stranger in the group is worse than an orphan in it.

**Report what it cannot identify.**  A grand-child that called
``setsid`` again inside a sub-agent has left the group the reaper
signalled. The heuristic (``cwd`` + ``cmdline`` + ``create_time`` over
``psutil.process_iter``) finds it, and the only thing done with that
finding is to *report* it — ``holder_unknown: true`` plus the list of
methods tried. It is never killed on the strength of a heuristic: a
heuristic that guesses wrong kills a stranger, and a stranger is worse
than an orphan.

The scan is anchored on the plan's **records**, not only on the
processes a sweep happened to kill: the ones that got away descend
from the plan's processes, and the plan's own record of them is what
says where they went. A sweep that killed nothing still has processes
to account for, and a sweep that let one go in silence reported a dead
tree.

And because the shape of a process can be faked — the same program in
the same directory has the same shape whoever started it — the scan
asks the kernel who started what. The plan's processes hang off the
process that owns the plan, so a candidate whose ancestors lead
nowhere near the plan is somebody else's, and naming it would be
crying wolf: a plan that looks like it has a survivor it does not have
is not a plan that gets stopped.

What this module writes
-----------------------
Named events into the ledger, so a reap is legible afterwards:

``killed``
    The process is gone. Carries ``pid``, ``role``, ``cmdline`` and
    ``cwd`` — a bare pid cannot be argued with after the fact.
``identity_mismatch``
    The pid is alive but is not the process that was recorded. Nothing
    was signalled.
``skipped``
    The process was deliberately not signalled (our own pid, our own
    process group, an unusable pid).
``failed``
    The process survived SIGTERM *and* SIGKILL — or the pid could not
    be vouched for, so the escalation was refused and the process was
    reported instead of killed.
``unknown``
    A process was found that might be an escaped descendant, and was
    deliberately not killed.

The module never raises for a process it cannot handle: a reap runs
during shutdown, and a shutdown that raises is a shutdown that stops
half way through.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import process_identity
import process_ledger as ledger
from utils.process import _signallable_pgid


logger = logging.getLogger(__name__)


#: The order in which roles are reaped. Children first.
#:
#: Fixed, and part of the contract rather than an implementation detail:
#: the whole point of the sweep is that a parent never outlives its
#: children. ``subagent`` comes before ``tool`` even though a tool is
#: usually the sub-agent's own child — a tool is also sometimes a
#: backend-owned helper that no sub-agent owns, and the order that
#: covers every shape of the tree is the one every layer agrees on.
REAP_ORDER: Tuple[str, ...] = (
    "subagent",
    "vp_service",
    "tool",
    "repair_executor",
    "executor",
)

#: The roles that are children of something else in the tree.
CHILD_ROLES: Tuple[str, ...] = ("subagent", "vp_service", "tool")

#: Event written when a process was terminated.
EVENT_KILLED = "killed"

#: Event written when a pid did not match the recorded identity.
EVENT_IDENTITY_MISMATCH = "identity_mismatch"

#: Event written when a process was deliberately not signalled.
EVENT_SKIPPED = "skipped"

#: Event written when a process survived the escalation.
EVENT_FAILED = "failed"

#: Event written when an escaped descendant was found but not killed.
EVENT_UNKNOWN = "unknown"

#: The methods tried, in order, before a survivor is reported as unknown.
#:
#: ``pgid`` — the recorded process group was signalled; anything still
#: alive in it is a survivor of a group kill, and anything outside it
#: escaped.
#: ``cwd+cmdline+create_time`` — the heuristic sweep that finds a
#: process whose group it is not in.
ESCAPE_TRIED: Tuple[str, ...] = ("pgid", "cwd+cmdline+create_time")

#: How far up the ancestor chain the escape scan looks.
#:
#: The chain is walked to decide whether a process that has the shape
#: of a descendant is actually one of the plan's. A process tree is
#: shallow in practice; the limit is there so that a pathological chain
#: cannot turn a sweep into a long walk.
_MAX_ANCESTRY = 32

#: Default grace window between SIGTERM and SIGKILL.
DEFAULT_GRACE_SECONDS = 10.0

#: How long to wait for a SIGKILLed process to disappear.
_KILL_GRACE_SECONDS = 2.0

#: How often liveness is polled while waiting.
_POLL_INTERVAL = 0.05


_psutil_module: Optional[Any] = None
_psutil_loaded = False


def _psutil() -> Optional[Any]:
    """The ``psutil`` module, or ``None`` when it is not installed.

    ``psutil`` is a dev dependency, not a runtime one, so the reaper
    has to work without it: the group kill, the identity check and the
    ledger all degrade gracefully, and only the escaped-descendant
    heuristic disappears.
    """
    global _psutil_module, _psutil_loaded
    if not _psutil_loaded:
        _psutil_loaded = True
        try:
            import psutil  # noqa: PLC0415 - deliberate late import
        except ImportError:  # pragma: no cover - the fallback covers it
            _psutil_module = None
        else:
            _psutil_module = psutil
    return _psutil_module


@dataclass
class ReapOutcome:
    """What one reap pass did to one plan.

    Attributes:
        plan_id: The plan that was swept.
        reason: Why the sweep was run (``stop``, ``server_shutdown``…).
        reaped: The pids that were terminated, in the order they were
            terminated. A process that was already gone is **not** here:
            it was never signalled, and nothing was reaped.
        skipped: Processes that were deliberately not signalled, each as
            ``{"pid", "role", "reason"}``.
        failed: Processes that survived SIGTERM *and* SIGKILL — or that
            the reaper could not vouch for and therefore refused to
            signal — each as ``{"pid", "role", "reason"}``.
        unknown: Processes that were found but not identified well
            enough to kill, each as
            ``{"pid", "holder_unknown", "tried"}``.
    """

    plan_id: str = ""
    reason: str = ""
    reaped: List[int] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    unknown: List[Dict[str, Any]] = field(default_factory=list)

    def clean(self) -> bool:
        """True when nothing of ours is left running that we know of.

        A *skipped* entry does not make the sweep unclean: skipping is
        a decision, not a leftover. The processes that count against
        cleanliness are the ones that are still running and could not
        be dealt with — a survivor of the escalation (``failed``) and a
        survivor the reaper could not even identify (``unknown``).
        """
        return not self.failed and not self.unknown

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable form of the outcome."""
        return {
            "plan_id": self.plan_id,
            "reason": self.reason,
            "reaped": list(self.reaped),
            "skipped": list(self.skipped),
            "failed": list(self.failed),
            "unknown": list(self.unknown),
        }


@dataclass
class _Anchor:
    """The facts the escaped-descendant sweep is anchored on.

    Two kinds of anchor exist, and they differ in where their facts come
    from:

    * a process the sweep **terminated** — the facts are the *live*
      ones, read from the kernel at the moment it was killed. The
      recorded command line may predate an ``argv[0]`` rewrite, and
      comparing a live reading against a stale one would find nothing;
    * a record the ledger **still holds** — the facts are the recorded
      ones. The process may be long gone (and then there is no live
      reading to take), and the recorded facts are all the ledger has.

    Either way the anchor says the same thing: a descendant of *this*
    would share its working directory and its program, and would have
    been born no earlier than it.
    """

    record: ledger.ProcessRecord
    cmdline: Tuple[str, ...]
    pgid: Optional[int]


@dataclass
class _Vouched:
    """The live facts a signal was authorised on.

    ``identity`` is the reading the kernel gave at the moment the
    signal was allowed, and ``pgid`` / ``group`` are the group the
    signal may be addressed to. It is returned by the signalling
    helper so that the caller records what it killed, not what it
    remembered from an earlier reading.
    """

    identity: process_identity.ProcessIdentity
    pgid: Optional[int]
    group: Optional[int]


def reap_plan(
    plan_dir: Any,
    plan_id: str = "",
    *,
    reason: str = "",
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    roles: Optional[Iterable[str]] = None,
    include_self: bool = False,
) -> ReapOutcome:
    """Reap every process the ledger says this plan still owns.

    The records are taken from :func:`process_ledger.live_records` and
    reaped in :data:`REAP_ORDER` — children first. For each one the
    identity is checked against the kernel, the process is signalled
    with SIGTERM, and the escalation to SIGKILL happens only after the
    grace window has passed. The identity is checked again before
    every signal, so a pid that was recycled during the grace window is
    reported rather than killed.

    Args:
        plan_dir: The plan's directory — where the ledger lives.
        plan_id: The plan's id. Defaults to the directory's name.
        reason: Why the sweep is running. It is written into the
            ledger events so a later reader can tell a stop from a
            shutdown from a sweep.
        grace_seconds: How long to wait after SIGTERM before
            escalating to SIGKILL.
        roles: Restrict the sweep to these roles. ``None`` means every
            role; a tuple narrows it, which is how the VP services are
            reaped without taking the executor down with them.
        include_self: Allow the reaper to signal **itself** — its own
            pid, and a recorded process that lives in its own process
            group. Off by default: those are the caller, and signalling
            the caller is how a sweep turns into an outage. The
            executor's own shutdown path turns it on, because that is
            the one caller that means "and me". The identity check
            still applies: a vouched-for pid is still a pid that gets
            recycled, so the recorded start time and command line have
            to agree before a signal is sent.

    Returns:
        A :class:`ReapOutcome` describing what happened. Never raises
        for a process it could not handle.
    """
    plan_path = Path(plan_dir)
    outcome = ReapOutcome(plan_id=plan_id or plan_path.name, reason=reason)

    live = ledger.live_records(plan_path)
    records = live
    if roles is not None:
        wanted = frozenset(str(role) for role in roles)
        records = {
            pid: record for pid, record in records.items()
            if record.role in wanted
        }

    handled: Set[int] = set()
    killed: List[_Anchor] = []
    for record in _ordered(records.values()):
        handled.add(record.pid)
        terminated = _reap_one(
            plan_path,
            record,
            outcome,
            grace_seconds=grace_seconds,
            include_self=include_self,
            # The **whole** live set, not the role-filtered one: the
            # question a group signal has to answer is "is every
            # member of this group one the plan's ledger vouches
            # for", and a member the sweep is not going to reap is
            # still a member the ledger knows about.
            records=live,
        )
        if terminated is not None:
            killed.append(terminated)

    # The escape scan is anchored on everything the plan still has on
    # the books, not only on what this sweep managed to kill. A sweep
    # that killed nothing has just as much to say about the processes
    # that got away: the plan's own record of a process is exactly the
    # thing that says where its descendants went.
    #
    # A process the sweep killed is anchored on the *live* facts it was
    # killed on; the rest are anchored on what the ledger recorded.
    anchors = list(killed)
    killed_pids = {anchor.record.pid for anchor in killed}
    for pid, record in live.items():
        if pid in killed_pids:
            continue
        anchors.append(_anchor_from_record(record))

    # A process the ledger mentions is *known*: it is either one this
    # sweep dealt with or one it deliberately left alone, and reporting
    # it as an unknown escapee would bury the real escapes under the
    # plan's own processes. The reaper is not a candidate either — it
    # is the one doing the asking.
    #
    # The reaper's own pid is known for a second reason: the plan's
    # processes hang off the process that owns the plan, so it is the
    # root of the tree the escape scan looks in.
    known = handled | set(live) | {os.getpid()}

    _report_escaped(plan_path, anchors, known, outcome)
    return outcome


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------

def _ordered(
    records: Iterable[ledger.ProcessRecord],
) -> List[ledger.ProcessRecord]:
    """The records in reap order: children first, then by age.

    A role the ledger does not know is put *first*: it is not a known
    parent of anything, so treating it as a leaf is the safe reading.
    """
    def sort_key(record: ledger.ProcessRecord) -> Tuple[int, float, int]:
        try:
            index = REAP_ORDER.index(record.role)
        except ValueError:
            index = -1
        return (index, float(record.create_time or 0.0), int(record.pid))

    return sorted(records, key=sort_key)


# ---------------------------------------------------------------------------
# one process
# ---------------------------------------------------------------------------

def _reap_one(
    plan_dir: Path,
    record: ledger.ProcessRecord,
    outcome: ReapOutcome,
    *,
    grace_seconds: float,
    include_self: bool,
    records: Mapping[int, ledger.ProcessRecord],
) -> Optional[_Anchor]:
    """Reap one recorded process. Returns the facts, if it was killed."""
    pid = record.pid
    role = record.role

    # A pid that cannot name a process is never a target. ``_signallable_pgid``
    # rejects the POSIX sentinels for groups; the same reasoning applies
    # to the pid itself.
    if pid <= 1:
        _skip(plan_dir, record, outcome, "unsafe_pid")
        return None

    if pid == os.getpid() and not include_self:
        _skip(plan_dir, record, outcome, "own_pid")
        return None

    # The SIGTERM. The identity is read from the kernel and checked
    # against the ledger inside the helper — the same helper the
    # escalation uses, so the two paths cannot drift apart.
    vouched = _signal_if_vouched(
        plan_dir,
        record,
        outcome,
        signal.SIGTERM,
        include_self=include_self,
        records=records,
    )
    if vouched is None:
        return None

    identity = vouched.identity
    live_pgid = vouched.pgid

    if not _wait_gone(pid, grace_seconds):
        # The grace window expired: the process ignored SIGTERM. The
        # identity is read *again* before the escalation — the window is
        # long enough for the pid to have been recycled, and a SIGKILL
        # sent on a remembered identity can land on a stranger.
        vouched = _signal_if_vouched(
            plan_dir,
            record,
            outcome,
            signal.SIGKILL,
            include_self=include_self,
            records=records,
        )
        if vouched is None:
            return None
        identity = vouched.identity
        live_pgid = vouched.pgid
        if not _wait_gone(pid, _KILL_GRACE_SECONDS):
            _fail(
                plan_dir,
                record,
                outcome,
                "still alive after SIGKILL",
                pgid=live_pgid,
            )
            return None

    outcome.reaped.append(pid)
    ledger.record_event(
        plan_dir,
        EVENT_KILLED,
        terminal=True,
        pid=pid,
        role=role,
        plan_id=outcome.plan_id,
        reason=outcome.reason,
        cmdline=list(identity.cmdline),
        cwd=identity.cwd or record.cwd,
        pgid=live_pgid,
    )
    return _Anchor(record=record, cmdline=identity.cmdline, pgid=live_pgid)


def _signal_if_vouched(
    plan_dir: Path,
    record: ledger.ProcessRecord,
    outcome: ReapOutcome,
    sig: int,
    *,
    include_self: bool,
    records: Mapping[int, ledger.ProcessRecord],
) -> Optional[_Vouched]:
    """Send ``sig`` to a recorded process — but only after checking who it is.

    This is the only place in the module where a signal is sent, and it
    is called once per signal. The identity is read from the kernel
    *every* time rather than remembered from the last one: a process
    that has already been signalled may have exited in the meantime,
    and its number may since have been handed to somebody else. The
    ledger's word is not enough — the kernel's start time and command
    line are what name a process.

    The check vouches for a *pid*.  A signal addressed to a *group*
    reaches every member of that group, so the group is only addressed
    as a whole when every member of it is vouch'd for as well; see
    :func:`_group_is_vouched`.  When the group cannot be addressed as a
    whole, the vouch'd process is signalled on its own — the tree is
    still collected, and the member that is not vouch'd for is left
    running.

    Args:
        plan_dir: The plan's directory — where the ledger lives.
        record: The record of the process to signal.
        outcome: The outcome the result is written into.
        sig: The signal to send.
        include_self: Whether the caller vouched for the reaper's own
            pid and process group.
        records: The plan's live records, keyed by pid — what says
            which pids the plan's ledger vouches for.

    Returns the live facts the signal was sent on, or ``None`` when
    nothing was sent: the reason has already been written to the ledger
    and to the outcome.
    """
    identity = process_identity.probe(record.pid)
    if identity is None:
        # The pid no longer names the process we recorded, so nothing
        # may be signalled on its word: a pid we cannot vouch for is
        # not a pid we may kill.  Whether the process is *gone* is a
        # separate question, and the liveness check answers it —
        # something may still be running under that number, and then the
        # sweep has to report it rather than pretend the tree is dead.
        if _alive(record.pid):
            _fail(
                plan_dir,
                record,
                outcome,
                "the pid no longer names the recorded process and could "
                "not be signalled",
            )
            return None
        # It is already gone. Nothing was reaped, but the record has to
        # leave the live set or every later sweep tries to collect it.
        ledger.record_exit(
            plan_dir,
            record.pid,
            ledger.EVENT_EXIT,
            detail="already gone at reap time",
        )
        return None

    if not _identity_matches(identity, record, allow_self=include_self):
        # The pid is alive but it is not the process we recorded. Send
        # nothing: "not known to be ours" is not a licence to kill.
        _skip(plan_dir, record, outcome, "identity_mismatch")
        return None

    own_pgid = os.getpgrp()
    live_pgid = (
        identity.pgid if identity.pgid is not None else (record.pgid or None)
    )
    group = _signallable_pgid(live_pgid)

    if group is not None and own_pgid is not None and group == own_pgid:
        if not include_self:
            # The process shares our group. Signalling the group would
            # signal the caller — the backend, or under pytest the whole
            # session.
            _skip(plan_dir, record, outcome, "own_process_group")
            return None
        # The caller vouched for its own group, but its group-mates are
        # not part of the request: only the process itself is signalled.
        group = None

    if group is not None and not _group_is_vouched(group, record, records):
        # The group holds a member the plan's ledger does not vouch
        # for — a pid whose live identity does not match its record
        # (a recycled pid), or a pid the ledger never mentioned.  A
        # group signal reaches every member of the group, so
        # signalling it would signal a member the identity check has
        # already refused.  The vouch'd process is signalled on its own
        # instead: a stranger is worse than an orphan.
        group = None

    _signal(record.pid, group, sig)
    return _Vouched(identity=identity, pgid=live_pgid, group=group)


# ---------------------------------------------------------------------------
# the group a signal would reach
# ---------------------------------------------------------------------------

def _group_is_vouched(
    group: int,
    record: ledger.ProcessRecord,
    records: Mapping[int, ledger.ProcessRecord],
) -> bool:
    """Whether every member of ``group`` is one the ledger vouches for.

    A group signal reaches every member of the group.  The identity
    check vouches for the *record* — one pid — and says nothing about
    the rest of the group, so the rest of the group has to be asked
    about too: each member is looked up in the plan's ledger and
    checked against it the same way the record itself is checked.

    A member that is not vouch'd for blocks the group signal.  It does
    not stop the sweep: the vouch'd process is still signalled, on its
    own, and the group is left alone.

    Args:
        group: The process group the signal would be addressed to.
        record: The record being signalled — its own pid is vouch'd
            for already, so it is not asked about again.
        records: The plan's live records, keyed by pid.

    Returns:
        ``True`` when the group may be signalled as a whole.
    """
    for pid in _group_members(group):
        if pid == record.pid:
            continue
        if not _member_is_vouched(pid, records):
            return False
    return True


def _member_is_vouched(
    pid: int,
    records: Mapping[int, ledger.ProcessRecord],
) -> bool:
    """Whether the plan's ledger vouches for the pid that is running now.

    A member the ledger does not mention is not vouch'd for.  Neither
    is one whose live identity does not match the record the ledger
    holds: that is the recycled pid, and refusing to signal it is the
    whole point of the check.

    A process that is gone — or a zombie, which has finished and is
    only waiting to be collected — is in no danger from a signal, so it
    does not block a group signal.
    """
    record = records.get(pid)
    if record is None:
        return False
    if not _alive(pid):
        return True
    identity = process_identity.probe(pid)
    if identity is None:
        # Alive, but with no readable identity: nothing vouches for it.
        return False
    return _identity_matches(identity, record, allow_self=False)


def _group_members(group: int) -> List[int]:
    """The pids that are in the process group ``group`` right now.

    The group a signal is addressed to is a *set* of processes, and a
    sweep that cannot name the set cannot say whether the signal is
    safe.  ``psutil`` is the only way to ask; without it the answer is
    an empty list, and an empty list means "the group holds nothing
    else" — the same degradation the rest of the module makes when
    ``psutil`` is not installed.
    """
    module = _psutil()
    if module is None:
        return []
    try:
        processes = list(module.process_iter(["pid"]))
    except Exception:  # noqa: BLE001 - a sweep must not raise
        return []

    members: List[int] = []
    for proc in processes:
        try:
            pid = int(proc.info["pid"])
        except Exception:  # noqa: BLE001 - the process may have gone
            continue
        try:
            candidate = os.getpgid(pid)
        except ProcessLookupError:
            # The process is gone: it is in no group.
            continue
        except OSError:
            # The group of a process owned by somebody else cannot be
            # read on every platform.  Such a process is in another
            # session, and a process in another session is not in this
            # group, so it is left out rather than guessed at.
            continue
        if candidate == group:
            members.append(pid)
    return members


def _identity_matches(
    identity: process_identity.ProcessIdentity,
    record: ledger.ProcessRecord,
    *,
    allow_self: bool,
) -> bool:
    """Whether the live process is the one the ledger recorded.

    :func:`process_identity.matches` refuses this process outright,
    which is right by default: the reaper must not signal its own
    caller. With ``include_self`` the caller has vouched for exactly
    that, so the same rules are applied minus that single refusal —
    the start time and the command line still have to agree, because
    a vouched-for pid is still a pid that gets recycled.
    """
    if allow_self and identity.pid == os.getpid():
        if identity.create_time is None or not record.create_time:
            return False
        if abs(
            float(identity.create_time) - float(record.create_time)
        ) > process_identity.CREATE_TIME_TOLERANCE:
            return False
        # The same comparison ``matches`` uses, reused rather than
        # re-implemented: two copies of the identity rules are two
        # rules that will drift apart.
        return process_identity._cmdline_matches(
            identity.cmdline, record.cmdline
        )
    return process_identity.matches(
        identity, create_time=record.create_time, cmdline=record.cmdline
    )


def _skip(
    plan_dir: Path,
    record: ledger.ProcessRecord,
    outcome: ReapOutcome,
    reason: str,
) -> None:
    """Record a process that was deliberately not signalled."""
    outcome.skipped.append({
        "pid": record.pid,
        "role": record.role,
        "reason": reason,
    })
    event = (
        EVENT_IDENTITY_MISMATCH if reason == "identity_mismatch"
        else EVENT_SKIPPED
    )
    ledger.record_event(
        plan_dir,
        event,
        terminal=False,
        pid=record.pid,
        role=record.role,
        plan_id=outcome.plan_id,
        reason=reason,
    )


def _fail(
    plan_dir: Path,
    record: ledger.ProcessRecord,
    outcome: ReapOutcome,
    reason: str,
    *,
    pgid: Optional[int] = None,
) -> None:
    """Record a process that is still there and could not be taken down.

    The survivor is *reported*, never signalled: it is either a process
    that outlived both signals, or a pid the reaper could not vouch
    for and therefore refused to touch. Either way something is still
    running under that number, and the sweep must not claim the tree
    is dead.
    """
    outcome.failed.append({
        "pid": record.pid,
        "role": record.role,
        "reason": reason,
    })
    ledger.record_event(
        plan_dir,
        EVENT_FAILED,
        terminal=False,
        pid=record.pid,
        role=record.role,
        plan_id=outcome.plan_id,
        reason=reason,
        pgid=record.pgid if pgid is None else pgid,
    )


# ---------------------------------------------------------------------------
# signalling
# ---------------------------------------------------------------------------

def _signal(pid: int, group: Optional[int], sig: int) -> None:
    """Send ``sig`` to the process group, or to the pid when there is none.

    The group is preferred: a sub-agent's tools live in its group, and
    killing the sub-agent alone leaves them running. ``group`` is
    ``None`` when the group must not be signalled — either because it is
    not a group this process may signal (the POSIX sentinels: ``kill(-1)``
    is a broadcast), because it is the caller's own, or because it holds
    a member the ledger does not vouch for.
    """
    if group is not None:
        try:
            os.killpg(group, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            # The group is gone, or we may not signal it. Fall through
            # to the pid: the process itself may still be signalable.
            pass
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _alive(pid: int) -> bool:
    """True when ``pid`` still names a running process.

    A zombie counts as dead: it has finished, and only its parent's
    bookkeeping is outstanding. The reaper's targets are usually not
    its children, but in a test they are, and a zombie that is waited
    for forever would make every reap look like a failure.
    """
    module = _psutil()
    if module is not None:
        try:
            status = module.Process(pid).status()
        except module.NoSuchProcess:
            return False
        except Exception:  # noqa: BLE001 - fall through to the POSIX probe
            pass
        else:
            return status != module.STATUS_ZOMBIE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; we are simply not allowed to signal it
    except OSError:
        return False
    return True


def _wait_gone(pid: int, timeout: float) -> bool:
    """Wait up to ``timeout`` seconds for ``pid`` to disappear."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if not _alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# escaped descendants
# ---------------------------------------------------------------------------

def _anchor_from_record(record: ledger.ProcessRecord) -> _Anchor:
    """The anchor for a record the ledger still holds.

    The facts are the *recorded* ones. The process may be long gone —
    and then there is no live reading to take at all — and the recorded
    facts are all the ledger has. A process that is still there has
    already been read by the sweep, and read carefully: the sweep only
    acts on a reading the kernel vouched for.
    """
    return _Anchor(
        record=record,
        cmdline=tuple(record.cmdline or ()),
        pgid=record.pgid or None,
    )


def _report_escaped(
    plan_dir: Path,
    anchors: Sequence[_Anchor],
    known: Set[int],
    outcome: ReapOutcome,
) -> None:
    """Report the processes that escaped the groups that were signalled.

    A grand-child that called ``setsid`` inside a sub-agent is not in
    the group the reaper signalled, so the group kill cannot reach it.
    The heuristic finds it — same working directory, same program, born
    after the process it came from — and the only thing done with that
    finding is to *report* it. Killing on a heuristic would kill a
    stranger who happened to run the same program in the same
    directory, and a stranger is worse than an orphan.

    The scan is anchored on **the plan's records**, not only on the
    processes this sweep killed. The processes that got away are the
    descendants of the plan's processes, and the plan's own record of
    those processes is what says where they went — a sweep that killed
    nothing still has processes to account for, and a sweep that let
    one go in silence is a sweep that reported a dead tree.

    Args:
        plan_dir: The plan's directory — where the ledger lives.
        anchors: The processes the escape is looked for around: the
            ones this sweep killed, plus the ones the ledger still
            holds.
        known: The pids that are the plan's own: the ones the ledger
            mentions, plus the reaper's own pid — the process that owns
            the plan, and so the root of the tree the plan's processes
            hang off.
        outcome: The outcome the report is written into.
    """
    module = _psutil()
    if module is None or not anchors:
        return

    candidates = _candidates(module)
    if not candidates:
        return

    reported: Set[int] = set()
    for entry in anchors:
        for info in candidates:
            pid = info.get("pid")
            if pid is None or pid in known or pid in reported:
                continue
            if not _looks_escaped(info, entry, module, known):
                continue
            reported.add(pid)
            outcome.unknown.append({
                "pid": pid,
                "holder_unknown": True,
                "tried": list(ESCAPE_TRIED),
            })
            ledger.record_event(
                plan_dir,
                EVENT_UNKNOWN,
                terminal=False,
                pid=pid,
                plan_id=outcome.plan_id,
                reason=outcome.reason,
                holder_unknown=True,
                tried=list(ESCAPE_TRIED),
            )


def _candidates(module: Any) -> List[Dict[str, Any]]:
    """A snapshot of the running processes, as psutil sees them.

    The working directory is deliberately **not** part of the snapshot:
    reading it is the most expensive thing psutil does here, and a
    sweep would pay for it once per process on the machine. It is read
    later instead, and only for the processes that already look like
    descendants — see :func:`_looks_escaped`.
    """
    try:
        processes = list(module.process_iter(
            ["pid", "cmdline", "create_time"]
        ))
    except Exception:  # noqa: BLE001 - a sweep must not raise
        return []

    snapshot: List[Dict[str, Any]] = []
    for proc in processes:
        try:
            info = proc.info
        except Exception:  # noqa: BLE001 - the process may have gone
            continue
        if isinstance(info, dict):
            snapshot.append(info)
    return snapshot


def _cwd_of(module: Any, pid: Any) -> Optional[str]:
    """The working directory of a process, or ``None`` when unreadable.

    Reading it is the most expensive thing this module does: on macOS
    psutil has to walk the vnode path to answer it. It is therefore
    read one process at a time, and only for the processes that already
    look like descendants — see :func:`_looks_escaped`.
    """
    if pid is None:
        return None
    try:
        return module.Process(int(pid)).cwd()
    except Exception:  # noqa: BLE001 - the process may have gone
        return None


def _looks_escaped(
    info: Dict[str, Any],
    entry: _Anchor,
    module: Any,
    known: Set[int],
) -> bool:
    """True when ``info`` looks like a descendant that escaped the group.

    Five conditions, all of which have to hold:

    * born no earlier than the process the anchor describes — a
      descendant cannot predate its ancestor, and this is what keeps the
      sweep from matching every unrelated process that shares the
      directory;
    * the same program — the same executable, read live from the
      kernel, not from the ledger;
    * a different process group — a process still inside the group that
      was signalled was reached by the group kill;
    * ours — the kernel still knows who started what, and the plan's
      processes hang off the process that owns the plan. See
      :func:`_attributable`;
    * the same working directory — a tool run by the process the anchor
      describes starts where it starts.

    The order is the cost: the directory is read last, because it is
    the one reading that costs, and by then the process has been
    narrowed down to the few that could be a descendant at all.
    """
    record = entry.record

    create_time = info.get("create_time")
    if create_time is None or float(record.create_time or 0.0) <= 0:
        return False
    if float(create_time) < float(record.create_time):
        return False

    cmdline = info.get("cmdline") or ()
    live = tuple(str(part) for part in cmdline if part)
    if not live or not entry.cmdline:
        return False
    if not _same_program(live, entry.cmdline):
        return False

    # A process still inside the group we signalled is not an escapee.
    if entry.pgid is not None:
        try:
            candidate_pgid = os.getpgid(int(info["pid"]))
        except (KeyError, OSError):
            return False
        if candidate_pgid == entry.pgid:
            return False

    # The shape above can be faked: the same program in the same
    # directory has the same shape whoever started it. The kernel knows
    # who started what, so the scan asks it.
    if not _attributable(module, info.get("pid"), known):
        return False

    cwd = _cwd_of(module, info.get("pid"))
    if not cwd or not record.cwd:
        return False
    if os.path.realpath(str(cwd)) != os.path.realpath(str(record.cwd)):
        return False

    return True


def _attributable(module: Any, pid: Any, known: Set[int]) -> bool:
    """True when the process may be one of the plan's own.

    The shape of a process cannot tell an escapee from a stranger: the
    same program in the same directory has the same shape whoever
    started it, and the shape is all a heuristic has. The kernel still
    knows who started what, and that is what settles the question:

    * an ancestor of the candidate is one of the plan's own processes,
      or the process that owns the plan — then the candidate is ours,
      however it got away;
    * the candidate's own parent is gone — the kernel re-parents an
      orphan to init — then the chain is broken, and the parent it lost
      may well have been one of ours;
    * the chain leads somewhere else entirely, to processes that are
      alive and are not ours — then the candidate belongs to somebody
      else, and naming it would be crying wolf;
    * the chain runs out before it reaches init — the top of the tree
      that can be seen from here — without passing through one of ours:
      that is the same answer, and it is spelled ``ppid == 0`` on
      Linux, where a process whose parent lives outside the pid
      namespace (a container's session leader, for one) reads as
      parentless.

    The answer is deliberately one-sided: whatever cannot be ruled out
    is kept, because a missed escapee is a process nobody will ever
    look for.
    """
    try:
        current = int(pid)
    except (TypeError, ValueError):
        return True

    seen: Set[int] = set()
    for depth in range(_MAX_ANCESTRY):
        if current in seen:
            # A cycle cannot happen in a process tree, but a sweep must
            # not raise.
            return True
        seen.add(current)
        try:
            parent = module.Process(current).ppid()
        except Exception:  # noqa: BLE001 - the process may have gone
            # The chain broke: the candidate may be an orphan of ours.
            return True
        if parent is None:
            # The ancestry is unknown, so it cannot be ruled out.
            return True
        if parent <= 0:
            # The top of the tree that can be seen from here. On Linux
            # a process whose parent lives outside the pid namespace
            # reads ``ppid == 0`` — a container's session leader, for
            # one — and that is the same place ``ppid == 1`` is on a
            # machine with an init: the end of the visible chain.
            if depth == 0:
                # The candidate's own parent is out of sight: it may be
                # an orphan of ours.
                return True
            # The chain reached the top and none of it is ours: a
            # stranger.
            return False
        if parent == 1:
            if depth == 0:
                # Init is where the kernel puts an orphan, so this
                # candidate lost its parent — and the parent it lost may
                # have been one of the plan's.
                return True
            # The chain reached the root of the tree and none of it is
            # ours: a stranger.
            return False
        if parent in known:
            # It hangs off the plan, or off the process that owns it.
            return True
        current = parent

    # A chain this long is not something to rule out.
    return True


def _same_program(live: Sequence[str], recorded: Sequence[str]) -> bool:
    """Whether two command lines name the same program.

    The two spellings are normally the same string, and then this is a
    plain comparison. They are not always: on macOS the launcher
    rewrites ``argv[0]`` shortly after the exec, so the same
    interpreter answers to ``.../venv/bin/python`` at one moment and to
    ``.../Python.app/Contents/MacOS/Python`` at the next. A command
    line recorded at one moment and read at another therefore names
    the same program two ways, and a comparison of the two spellings
    alone would call them different programs — which is how a recorded
    process fails to be recognised in the process that came after it.

    The rewrite touches the executable and nothing else, so two command
    lines that agree on everything *after* the executable name the same
    program as well. That is the second reading, and it needs at least
    one argument to agree on: two command lines that carry nothing but
    an executable say nothing about the program behind it.
    """
    if not live or not recorded:
        return False
    if live[0] == recorded[0]:
        return True
    if len(live) < 2 or len(recorded) < 2:
        return False
    return list(live[1:]) == list(recorded[1:])


# ---------------------------------------------------------------------------
# the executor's own signal handlers
# ---------------------------------------------------------------------------
#
# An executor that is killed leaves its tree behind. Every layer of
# PDT's tree detaches from its parent on purpose — a sub-agent in its
# own session, a VP service in another — so the executor's process
# group holds none of them, and a ``kill`` on the executor reaches the
# executor and nothing else.
#
# The executor is therefore the last process that knows the tree
# exists, and this is where it says goodbye: the ledger is swept (child
# before parent), the single-instance lock is released, and the exit
# code says which signal asked.

#: The exit code a process terminated by SIGTERM reports: ``128 + 15``,
#: the shell's own convention for "killed by SIGTERM".
EXIT_SIGTERM = 143

#: The exit code for SIGINT: ``128 + 2``.
EXIT_SIGINT = 130

#: The signals the executor answers, and the exit code each one leaves
#: with. A tuple of pairs rather than a mapping, so that the order the
#: handlers are installed in is the order of this table.
EXECUTOR_SIGNALS: Tuple[Tuple[int, int], ...] = (
    (signal.SIGTERM, EXIT_SIGTERM),
    (signal.SIGINT, EXIT_SIGINT),
)


def install_executor_signal_handlers(
    plan_dir: Any,
    plan_id: str = "",
    *,
    lock: Optional[Any] = None,
) -> None:
    """Install the executor's own SIGTERM and SIGINT handlers.

    The handlers answer the two signals that mean "stop" by collecting
    the plan's process tree out of the ledger, releasing the
    single-instance lock, and leaving with an exit code that says which
    signal asked. Without them the executor dies on the spot and
    everything it started — a sub-agent with its tools, a VP service
    with its port — stays behind with nobody left to own it.

    The order inside the handler is fixed and it is the order that
    matters: **sweep, release, exit**. The sweep goes first because the
    lock is what says "this plan is being worked on", and releasing it
    before the tree is collected would let a second executor start
    against a tree that is still running. The exit goes last because it
    is the only step that ends the process.

    Args:
        plan_dir: The plan's directory — where the ledger lives.
        plan_id: The plan's id. Defaults to the directory's name.
        lock: The executor's single-instance lock, if it holds one.
            Anything with a ``release()`` method will do; it is
            released after the sweep and before the exit.

    The handlers are installed on the **main thread only** — that is
    the only thread the operating system will deliver a signal to.
    Called from anywhere else the installation is skipped with a
    warning, and the executor carries on: a plan that refuses to start
    because its signals cannot be handled is a worse outcome than a
    plan whose signals are handled by the default action.

    Nothing is installed at import time. The backend runs under
    uvicorn, which owns the signal handlers of the process it is in;
    a handler installed by an import would replace the server's own.
    So the installer is called by the executor's entry point, and by
    nothing else.

    A **second** signal does not start a second sweep. The first one
    is still collecting the tree, and sweeping again would signal
    processes the first sweep has already collected; the second signal
    leaves with the same exit code and without collecting anything
    twice.
    """
    if threading.current_thread() is not threading.main_thread():
        logger.warning(
            "[executor] the signal handlers were not installed: signals can "
            "only be installed from the main thread (running on %s)",
            threading.current_thread().name,
        )
        return

    plan_path = Path(plan_dir)

    # One flag for both handlers, and it is what makes a second signal
    # a no-op instead of a second sweep.
    state: Dict[str, bool] = {"reaping": False}

    for signum, exit_code in EXECUTOR_SIGNALS:
        handler = _executor_signal_handler(
            plan_path, plan_id, lock, exit_code, state
        )
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError, RuntimeError) as exc:
            # A signal this platform does not have, or one the platform
            # refuses to hand over. The executor still runs; it just
            # will not answer that signal politely.
            logger.warning(
                "[executor] the %s handler was not installed: %s",
                _signal_name(signum),
                exc,
            )


def _executor_signal_handler(
    plan_path: Path,
    plan_id: str,
    lock: Optional[Any],
    exit_code: int,
    state: Dict[str, bool],
) -> Callable[[int, Any], None]:
    """Build the handler for one of the executor's signals.

    ``state`` is shared by the handlers one installation creates, so
    that "a signal arrived while a sweep was running" is a fact about
    the installation rather than about one signal.
    """

    def _handler(signum: int, frame: Any) -> None:
        if state.get("reaping"):
            # A second signal arrived while the first sweep was still
            # collecting the tree. Re-entering the sweep would signal
            # processes the first sweep has already collected, and would
            # wait out a second grace window on top of the first one.
            # The exit code is the one the first signal chose.
            raise SystemExit(exit_code)

        state["reaping"] = True
        reason = "executor_{}".format(_signal_name(signum))
        try:
            # ``include_self=False``: the executor is the caller, and a
            # sweep that signals its own caller is how a collection
            # turns into an outage. The executor's own exit is the
            # SystemExit below.
            reap_plan(plan_path, plan_id, reason=reason, include_self=False)
        except Exception:  # noqa: BLE001 - a shutdown that raises stops half way
            logger.exception("[executor] the %s sweep failed", reason)
        finally:
            if lock is not None:
                try:
                    lock.release()
                except Exception:  # noqa: BLE001 - the exit must still happen
                    logger.exception(
                        "[executor] the %s sweep could not release the lock",
                        reason,
                    )
        raise SystemExit(exit_code)

    return _handler


def _signal_name(signum: int) -> str:
    """The lowercase name of ``signum``, for a reason written to the ledger."""
    try:
        return signal.Signals(signum).name.lower()
    except (TypeError, ValueError):
        return str(signum)


__all__ = [
    "CHILD_ROLES",
    "ESCAPE_TRIED",
    "EVENT_FAILED",
    "EVENT_IDENTITY_MISMATCH",
    "EVENT_KILLED",
    "EVENT_SKIPPED",
    "EVENT_UNKNOWN",
    "EXECUTOR_SIGNALS",
    "EXIT_SIGINT",
    "EXIT_SIGTERM",
    "REAP_ORDER",
    "ReapOutcome",
    "install_executor_signal_handlers",
    "reap_plan",
]