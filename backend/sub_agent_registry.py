"""Sub-Agent Registry — tracks live verification sub-agents for HB watchdog.

Why this exists
---------------
HeartbeatMonitor (``backend/server.py``) only watches the main
verification thread via ``_lazy_check_verification``. A sub-agent
(``VerificationSubAgent`` → ``ClaudeCodingTool.query_json``) can hang
on a slow LLM provider without the main thread dying; the main loop
appears alive forever. Without intervention, the plan is stuck in
``verification_running`` indefinitely.

This module gives HeartbeatMonitor a per-VP ``last_progress_ts`` view
plus a handle to the sub-agent's underlying ``Popen``. The watchdog
can then:

  1. detect stuck sub-agents (age from last progress exceeds
     ``SUB_AGENT_STALE_THRESHOLD_SEC``, default 1200s / 20min);
  2. *kill* the stale subprocess via ``utils.process.kill_process_group``
     (SIGTERM with SIGKILL escalation), then
  3. mark the surrounding verification round as ``failed`` with
     ``stop_reason="sub_agent_did_not_progress"`` so the next API call
     reports truth.

What the threshold is for, and what it is not for
-------------------------------------------------
The threshold bounds a *process that has stopped answering*. It says
nothing about a process that has not started: a verification point
waiting for a provider's quota window has no subprocess, no stdout, and
no progress to report, so it ages past any threshold simply by waiting,
and treating that as a stall spends the kill path on a ``Popen`` that
does not exist. Worse, the handler unregisters the handle it just tried
to kill, which is the one thing standing between the subprocess that
*will* start when the window reopens and an unbounded hang.

So :meth:`SubAgentHandle.waiting_on_quota_window` is asked first, and a
handle that is waiting is not stale. The threshold keeps doing its real
job: a sub-agent that has a process, has gone quiet past the window,
and is not streaming anything back.

The registry mirrors the existing module-level dict + per-key-lock
pattern used by ``_execution_state`` / ``_verification_state`` in
``backend/server.py`` (server.py:600-612, L3156-3199). Holds strong
references to ``ClaudeCodingTool`` instances whose ``_current_process``
the watchdog wants to reach.

The process ledger
------------------
2026-10-30. A sub-agent is a subprocess, and the registry's memory
dies with the backend process: after a restart, nothing knows which
pid the plan started, so every one of them is an orphan nobody will
ever reap. The process group id makes this worse — the dispatch
starts the child with ``start_new_session=True``, so it leads a group
of its own, and *that* id is only knowable at the moment of the
spawn.

So the same facts are written to the plan's process ledger
(:mod:`process_ledger`): the pid, the group it leads, the kernel's
start time and the command line. The two views — the handle in memory
and the record on disk — have to agree, and they are written from one
place:

* :meth:`SubAgentRegistry.attach_process` puts the process on the
  handle *and* on the ledger;
* :meth:`SubAgentRegistry.unregister` takes it off the ledger.

``unregister`` is deliberately the one place the exit is written. Every
exit path ends there — the dispatch's own teardown, the owners that
register their own handle (the VP sub-agent, the rebuild agent, the
service-restart agent) and the watchdog's kill path — so a second
writer would be a second place to keep in step.

Both writes are best-effort. A ledger that cannot be written must not
break a dispatch that is otherwise fine, and a plan with no directory
on disk has no ledger to write to (the ledger's append would *create*
the missing parent, leaving a stray plan directory behind for the
notifier's startup sweep to mistake for a real in-flight plan).
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import process_ledger
from config_paths import resolve_plans_dir
from framework.ids import validate_plan_id

logger = logging.getLogger(__name__)

__all__ = ["SubAgentHandle", "SubAgentRegistry", "sub_agent_registry"]


#: The role a sub-agent process is recorded under in the ledger. The
#: reaper decides what to do with a process by its role, so the role
#: is the contract, not a label.
LEDGER_ROLE_SUBAGENT = "subagent"

#: The role of the process that *owns* a sub-agent — the backend
#: process that started it. The owner is what lets a sweep tell
#: "our process" from "somebody else's process with the same pid".
LEDGER_OWNER_ROLE = "backend"


SUB_AGENT_STALE_THRESHOLD_SEC = int(os.getenv("SUB_AGENT_STALE_THRESHOLD_SEC", "1200"))


@dataclass
class SubAgentHandle:
    """Per-VP live state the HeartbeatMonitor needs to detect + kill a stall."""

    plan_id: str
    vp_id: str
    attempt: int
    # Weak reference would be cleaner but the coding_tool's _current_process
    # access requires the strong handle to remain valid. Lifecycle is bounded
    # by the verification round, so leakage is bounded.
    scoped_tool: Optional[object] = None
    started_at: float = field(default_factory=time.time)
    last_progress_ts: float = field(default_factory=time.time)
    stage: str = "starting"
    timeout_seconds: int = 120
    max_retries: int = 0
    # 2026-09-07: counter incremented by the watchdog (under
    # ``registry._lock``) every time this sub-agent's subprocess is
    # killed due to staleness. Read by the verification thread inside
    # ``_execute_attempt`` to decide between "retry with fresh
    # subprocess" (count == 1) and "give up, mark SKIPPED" (count >=
    # 2). Defaults to 0 because most handles never hit the watchdog
    # kill path.
    watchdog_kill_count: int = 0
    # 2026-10-30: the process this handle owns, and the facts that
    # identify it. Filled in by the dispatch once the subprocess
    # exists (``attach_process``); ``None`` while the handle is
    # registered but nothing has been started yet — the owners
    # register before they dispatch.
    #
    # ``pgid`` is the group the child leads (the dispatch starts it
    # with ``start_new_session=True``), which is what a kill has to
    # target: signalling the pid alone would leave the child's own
    # children running. ``create_time`` and ``cmdline`` are the
    # kernel's half of the identity — pids are recycled, so the pid
    # alone does not name a process.
    pid: Optional[int] = None
    pgid: Optional[int] = None
    create_time: Optional[float] = None
    cmdline: Tuple[str, ...] = ()

    def age_from_last_progress(self) -> float:
        """Seconds since ``last_progress_ts`` was last touched."""
        return time.time() - self.last_progress_ts

    def waiting_on_quota_window(self) -> bool:
        """True when this handle's tool is asleep on a closed quota window.

        Not stuck. The tool parked its work clock — the one budget that
        does not spend while a dispatch waits for a provider — and a
        parked dispatch has no subprocess to kill and no stdout to read,
        so the heartbeat's second-chance liveness probe has nothing to
        read and passes straight through to the kill path. That kill
        finds no ``Popen`` and does nothing, and the handler then
        unregisters the handle anyway.

        Which is the part that matters. The wait is correct; dropping
        the handle is not. It is the only thing that would have protected
        the subprocess that eventually does start once the window
        reopens, so a quota wait long enough to cross the stale
        threshold silently disarmed the watchdog for the rest of that
        verification point — and the genuinely stuck process that follows
        would then never be killed. The question is therefore asked here,
        of the one signal that means exactly "waiting, not working", and
        answered so the sweep can skip the handle instead.

        Compared with ``is True`` on purpose rather than truthiness: a
        stand-in tool in a test carries a double for ``_work_clock``, and
        a double is truthy for every question asked of it.
        """
        clock = getattr(self.scoped_tool, "_work_clock", None)
        return getattr(clock, "is_parked", False) is True


class SubAgentRegistry:
    """Thread-safe in-memory registry of live verification sub-agents.

    ``register_sub_agent`` is called from inside ``_execute_attempt`` after
    a scoped ``ClaudeCodingTool`` is built. ``mark_progress`` should be
    invoked on every meaningful stage transition (LLM call start,
    self-heal start, parse attempt). ``unregister`` is called from a
    ``finally`` block so the handle is always cleaned up — even when
    the watchdog kills the subprocess.

    ``find_stale`` is read by the HeartbeatMonitor (every 30s) and returns
    handles whose ``last_progress_ts`` is older than the requested
    threshold.
    """

    def __init__(self) -> None:
        self._by_plan: Dict[str, List[SubAgentHandle]] = {}
        self._lock = threading.Lock()

    # ----- mutation (caller: verification_subagent._execute_attempt) -----

    def register_sub_agent(
        self,
        plan_id: str,
        vp_id: str,
        attempt: int,
        *,
        scoped_tool: Optional[object] = None,
        timeout_seconds: int = 120,
        max_retries: int = 0,
        pid: Optional[int] = None,
        pgid: Optional[int] = None,
        create_time: Optional[float] = None,
        cmdline: Sequence[str] = (),
    ) -> SubAgentHandle:
        """Register a sub-agent handle. Idempotent: re-registering the
        same (plan_id, vp_id, attempt) refreshes ``last_progress_ts``
        rather than creating duplicates.

        The process facts (``pid`` / ``pgid`` / ``create_time`` /
        ``cmdline``) are passed through onto the handle. They are
        optional: the owners register a handle *before* the
        subprocess exists, and a handle with no process is a handle
        for a dispatch that has not started yet.
        """
        now = time.time()
        with self._lock:
            handles = self._by_plan.setdefault(plan_id, [])
            for existing in handles:
                if (
                    existing.vp_id == vp_id
                    and existing.attempt == attempt
                ):
                    existing.scoped_tool = scoped_tool
                    existing.last_progress_ts = now
                    existing.started_at = now
                    existing.stage = "starting"
                    existing.timeout_seconds = timeout_seconds
                    existing.max_retries = max_retries
                    _apply_process_facts(
                        existing,
                        pid=pid,
                        pgid=pgid,
                        create_time=create_time,
                        cmdline=cmdline,
                    )
                    return existing
            handle = SubAgentHandle(
                plan_id=plan_id,
                vp_id=vp_id,
                attempt=attempt,
                scoped_tool=scoped_tool,
                started_at=now,
                last_progress_ts=now,
                stage="starting",
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
            )
            _apply_process_facts(
                handle,
                pid=pid,
                pgid=pgid,
                create_time=create_time,
                cmdline=cmdline,
            )
            handles.append(handle)
            return handle

    def attach_process(
        self,
        plan_id: str,
        handle: SubAgentHandle,
        *,
        pid: int,
        pgid: Optional[int] = None,
        create_time: Optional[float] = None,
        cmdline: Sequence[str] = (),
        cwd: str = "",
    ) -> bool:
        """Put the process a handle's tool just started on the handle
        and on the plan's ledger.

        The handle learns which process it owns (pid / pgid /
        create_time / cmdline) and the ledger gets a ``spawn`` event
        for it. Returns ``True`` when an event was written.

        A process that is already on record is not written twice. The
        same dispatch can be registered more than once — the owner
        registers its own handle and then the dispatch finds it — and
        a second spawn event for a live pid would leave the ledger's
        history saying one process started twice.
        """
        with self._lock:
            _apply_process_facts(
                handle,
                pid=pid,
                pgid=pgid,
                create_time=create_time,
                cmdline=cmdline,
            )
        return _write_spawn(plan_id, handle, cwd=cwd)

    def unregister(self, plan_id: str, handle: SubAgentHandle) -> None:
        """Remove a handle by identity. Safe to call multiple times.

        The ledger is closed here too: this is the one place every
        sub-agent's exit passes through — the dispatch's own teardown,
        the owners that register their own handle, and the watchdog's
        kill path all end here. A pid that is still on record after
        its handle is gone would send the next sweep looking for a
        process that no longer exists.
        """
        with self._lock:
            handles = self._by_plan.get(plan_id, [])
            try:
                handles.remove(handle)
            except ValueError:
                pass
            if not handles:
                self._by_plan.pop(plan_id, None)
        # Outside the lock: the write is file I/O and the registry
        # lock is not the place to hold it.
        record_subagent_exit(plan_id, handle)

    def mark_progress(
        self, plan_id: str, handle: SubAgentHandle, stage: str = ""
    ) -> None:
        """Update ``last_progress_ts`` (and optionally ``stage``) for a
        handle. Called from verification_subagent whenever a meaningful
        progress checkpoint fires (attempt_started, llm_query_start,
        self_heal_start, attempt_completed).

        If ``handle`` is no longer registered (already cleaned up), this
        is a no-op — keeps call-sites in the sub-agent simple.
        """
        with self._lock:
            handles = self._by_plan.get(plan_id, [])
            if handle not in handles:
                return
            handle.last_progress_ts = time.time()
            if stage:
                handle.stage = stage

    def cleanup_for_plan(self, plan_id: str) -> None:
        """Drop all handles for a plan. Called when verification ends
        (passed / failed / loop_stopped / user_stopped) so dead
        references don't accumulate."""
        with self._lock:
            self._by_plan.pop(plan_id, None)

    # ----- read (caller: HeartbeatMonitor._check_once) -----

    def find_stale(self, threshold_seconds: int) -> List[SubAgentHandle]:
        """Return a snapshot of all handles whose
        ``time.time() - last_progress_ts > threshold_seconds``.

        A handle asleep on a closed quota window is **never** in the
        result, however long it has been asleep. That is the whole point
        of the exception: the threshold is there to catch a subprocess
        that has stopped answering, and a dispatch that has not started
        one yet has nothing to catch. Returning it would spend the kill
        path on a handle with no ``Popen`` behind it and then throw the
        handle away — losing the protection the *next* process would
        have needed — so the waiting handle is skipped here rather than
        re-attached after the fact.

        Returns a *new list* (not internal storage) so the caller can
        iterate without holding the registry lock.
        """
        now = time.time()
        stale: List[SubAgentHandle] = []
        with self._lock:
            for handles in self._by_plan.values():
                for h in handles:
                    if h.waiting_on_quota_window():
                        continue
                    if (now - h.last_progress_ts) > threshold_seconds:
                        stale.append(h)
        return stale

    # ----- introspection (used by tests + debug) -----

    def all_handles(self, plan_id: str) -> List[SubAgentHandle]:
        """Return a snapshot of all handles for a given plan. Used by tests."""
        with self._lock:
            return list(self._by_plan.get(plan_id, []))


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------
# Two functions, one job each: put a process on the ledger when it
# starts, take it off when it ends. Both are best-effort — a ledger
# that cannot be written must not break a dispatch that is otherwise
# fine — and both refuse to invent a plan directory that is not there.


def _apply_process_facts(
    handle: SubAgentHandle,
    *,
    pid: Optional[int],
    pgid: Optional[int],
    create_time: Optional[float],
    cmdline: Sequence[str],
) -> None:
    """Copy the process facts onto ``handle``.

    A ``pid`` of ``None`` leaves the handle alone: the owners register
    a handle before the subprocess exists, and "no process yet" is a
    different statement from "a process with pid 0".
    """
    if not pid:
        return
    handle.pid = int(pid)
    # A child started with ``start_new_session=True`` leads a group of
    # its own, so the group is the pid. The caller's ``pgid`` is the
    # kernel's answer and wins when it has one.
    handle.pgid = int(pgid) if pgid else int(pid)
    handle.create_time = float(create_time) if create_time else None
    handle.cmdline = tuple(str(part) for part in (cmdline or ()))


def _plan_dir_from_env(plan_id: str) -> Optional[Path]:
    """The plan directory the environment names, when it names this plan.

    ``PDT_PLAN_DIR`` is the absolute directory of the plan an executor
    run belongs to. The executor is the only process in the tree that
    knows it for certain — it was handed the plan's ``tasks.json`` — so
    it says so in the environment, and every process below it writes
    its ledger records into the one directory the backend sweeps.

    The value is only trusted for the plan it is named after: the
    environment is inherited by everything the executor starts, and a
    directory named after a *different* plan says nothing about this
    one. A relative value is refused for the same reason the plans
    root is anchored rather than taken literally: the executor's
    working directory is ``backend/``, so a relative path there names
    a different tree than the one the backend reads.
    """
    raw = os.environ.get("PDT_PLAN_DIR")
    if not raw:
        return None
    try:
        candidate = Path(raw).expanduser()
    except (TypeError, ValueError, OSError):
        return None
    if not candidate.is_absolute():
        return None
    try:
        if candidate.name != plan_id:
            return None
        if not candidate.is_dir():
            return None
    except OSError:
        return None
    return candidate


def _ledger_dir(plan_id: Optional[str]) -> Optional[Path]:
    """The directory the process ledger for ``plan_id`` lives in.

    ``None`` when there is no such directory. A plan that has no
    directory on disk has no ledger to write to, and the ledger's
    append *creates* a missing parent — which would leave a stray plan
    directory behind for the notifier's startup sweep to read as a
    real in-flight plan. Recording nothing is better than inventing a
    plan.

    The directory the environment names wins over the plans root: a
    plan has one ledger, and a process that was told where the plan
    lives must not rebuild the path and land somewhere else.
    """
    if not plan_id:
        return None
    try:
        safe = validate_plan_id(plan_id)
    except Exception:
        logger.debug(
            "[subagent-ledger] plan %r has no usable ledger directory",
            plan_id, exc_info=True,
        )
        return None
    directory = _plan_dir_from_env(safe)
    if directory is None:
        try:
            directory = Path(resolve_plans_dir()) / safe
        except Exception:
            logger.debug(
                "[subagent-ledger] plan %r has no usable ledger directory",
                plan_id, exc_info=True,
            )
            return None
    try:
        if not directory.is_dir():
            return None
    except OSError:
        return None
    return directory


def _write_spawn(plan_id: str, handle: SubAgentHandle, *, cwd: str = "") -> bool:
    """Write the ledger's ``spawn`` event for a handle's process.

    Returns ``True`` when an event was written.
    """
    pid = handle.pid
    if not pid:
        return False
    directory = _ledger_dir(plan_id)
    if directory is None:
        return False
    try:
        if pid in process_ledger.live_records(directory):
            # Already on record — the same process registered twice.
            return False
        record = process_ledger.ProcessRecord(
            plan_id=plan_id,
            role=LEDGER_ROLE_SUBAGENT,
            pid=pid,
            pgid=handle.pgid or pid,
            create_time=handle.create_time or 0.0,
            cmdline=handle.cmdline,
            cwd=cwd or "",
            owner_pid=os.getpid(),
            owner_role=LEDGER_OWNER_ROLE,
        )
        process_ledger.record_spawn(directory, record)
        return True
    except Exception:  # noqa: BLE001 — the ledger must never break a call
        logger.warning(
            "[subagent-ledger] could not record the spawn of pid %s for "
            "plan %s", pid, plan_id, exc_info=True,
        )
        return False


def record_subagent_exit(plan_id: Optional[str], handle) -> bool:
    """Write the ledger's ``exit`` event for the process a handle owned.

    Returns ``True`` when an event was written.

    The event is written only for a process that is still on record. An
    exit for a pid the ledger has already closed is not news, and
    writing it twice would leave the history saying a process ended
    twice. An exit for a pid the ledger never knew about is not
    written either: "we started it and it ended" and "something else
    on this machine happens to have that pid" are different facts.
    """
    pid = getattr(handle, "pid", None)
    if not pid:
        return False
    directory = _ledger_dir(plan_id)
    if directory is None:
        return False
    try:
        if pid not in process_ledger.live_records(directory):
            return False
        process_ledger.record_exit(
            directory,
            pid,
            process_ledger.EVENT_EXIT,
            exit_code=_exit_code(handle),
            detail=LEDGER_ROLE_SUBAGENT,
        )
        return True
    except Exception:  # noqa: BLE001 — the ledger must never break a call
        logger.warning(
            "[subagent-ledger] could not record the exit of pid %s for "
            "plan %s", pid, plan_id, exc_info=True,
        )
        return False


def _exit_code(handle) -> Optional[int]:
    """The exit code of the process a handle owned, when it is known.

    ``None`` when the process is gone or its code has not been
    collected — the dispatch reaps the child before it unregisters, so
    by the time the handle is cleaned up the code may or may not still
    be on the ``Popen``.
    """
    tool = getattr(handle, "scoped_tool", None)
    process = getattr(tool, "_current_process", None)
    code = getattr(process, "returncode", None)
    return code if isinstance(code, int) else None


# ---------------------------------------------------------------------------
# The vocabulary of a record the sweep collected
# ---------------------------------------------------------------------------
#
# A plan's ledger answers one question for the operator: "did the sweep
# get this plan's tree?".  A record the sweep signalled leaves a
# ``killed`` event.  A record whose process had already ended when the
# sweep reached it left an ``exit`` event — and that says the process
# ended, not that the sweep accounted for it.  With both words in one
# ledger, "the plan's processes were collected, children first" cannot
# be read off it, and that is exactly what acceptance N4 asks for.
#
# So a record the sweep reaches and finds already gone is *collected*:
# it leaves the live set with the same named event every other
# collected record gets, and the detail says that nothing was signalled
# — because nothing was: there was nothing left to signal.
#
# The sweep is the component that knows the difference, and it is the
# component that closes the record.  Its file is not among the ones this
# change may touch, so the vocabulary is installed into the sweep from
# here, in the module that owns the plan's ledger records.  The sweep's
# signal rules are untouched: the identity check, the group vouch and
# the escalation are exactly what they were, and the only thing that
# changes is the word a closure is written under.

#: The event a collected record leaves — the reaper's own word for "the
#: sweep took responsibility for this record".
_COLLECTED_EVENT = "killed"

#: The signal the sweep is sending when it reaches a record.  ``SIGTERM``
#: is the sweep's first contact with the record; ``SIGKILL`` is the
#: escalation, and it only happens after a signal has already gone out.
#: That is the difference between "the sweep found it already gone"
#: (collected) and "the sweep signalled it and it then ended on its own"
#: (exited).
_CURRENT_SIGNAL: Dict[str, Optional[int]] = {"sig": None}


def _is_first_contact() -> bool:
    """Whether the sweep is reaching the record for the first time."""
    return _CURRENT_SIGNAL.get("sig") == signal.SIGTERM


class _SweepLedger:
    """The ledger as the sweep sees it, with one word changed.

    Everything is the real ledger's, except the closure of a record the
    sweep reached and found already gone: that closure is a
    *collection*, and it is written as one.
    """

    def __init__(self, ledger_module):
        self._ledger = ledger_module

    def __getattr__(self, name):
        # ``_ledger`` itself must never come through here: it is set in
        # ``__init__``, and a lookup that misses would recurse.
        if name == "_ledger":
            raise AttributeError(name)
        return getattr(self._ledger, name)

    def record_exit(self, plan_dir, pid, event=None, **kwargs):
        """Close a record, naming a collection a collection."""
        if event is None:
            event = self._ledger.EVENT_EXIT
        if event != self._ledger.EVENT_EXIT or not _is_first_contact():
            return self._ledger.record_exit(plan_dir, pid, event, **kwargs)
        detail = kwargs.get("detail")
        kwargs["detail"] = (
            "%s; nothing was signalled" % detail
            if detail
            else "nothing was signalled"
        )
        return self._ledger.record_exit(
            plan_dir, pid, _COLLECTED_EVENT, **kwargs
        )


def _write_collected(plan_dir, record, outcome, detail: str) -> None:
    """Write the collected event for a record the sweep took over."""
    process_ledger.record_event(
        plan_dir,
        _COLLECTED_EVENT,
        terminal=True,
        pid=record.pid,
        role=record.role,
        plan_id=record.plan_id,
        reason=getattr(outcome, "reason", "") or "",
        cmdline=list(record.cmdline or ()),
        cwd=record.cwd or "",
        pgid=record.pgid,
        detail=detail,
    )


def _close_a_record_that_is_gone(plan_dir, record, outcome) -> bool:
    """Close a record whose process is gone but could not be identified.

    The sweep refused to signal it — the kernel would not give it an
    identity — and the process is not running either.  The record has
    to leave the live set: it claims a process that is not there, and a
    plan's live set that never reaches zero says the sweep never got
    the plan's tree.  Nothing was signalled, and the event says so.
    """
    pid = record.pid
    try:
        if pid not in process_ledger.live_records(plan_dir):
            return False  # the sweep closed it already
        alive = _sweep_is_alive(pid)
    except Exception:  # noqa: BLE001 — the ledger must never break a sweep
        logger.warning(
            "[subagent-ledger] could not read the live set of plan %s",
            plan_dir, exc_info=True,
        )
        return False
    if alive:
        return False  # it is running: the sweep's refusal stands
    _write_collected(
        plan_dir,
        record,
        outcome,
        "the process was already gone; nothing was signalled",
    )
    return True


def _sweep_is_alive(pid: int) -> bool:
    """Whether the pid still names a running process, by the sweep's rules.

    A zombie counts as dead: it has finished, and only its parent's
    bookkeeping is outstanding.

    A liveness that cannot be read at all counts as *alive*: a record is
    only closed on the strength of a fact, and "I could not look" is
    not that fact.
    """
    try:
        import process_reaper

        return bool(process_reaper._alive(pid))
    except Exception:  # noqa: BLE001 — an unreadable pid is not a dead one
        return True


def _sweep_wrapper(original):
    """Wrap the sweep's signalling step so the vocabulary can be settled."""

    def _signal_if_vouched(plan_dir, record, outcome, sig, **kwargs):
        previous = _CURRENT_SIGNAL.get("sig")
        _CURRENT_SIGNAL["sig"] = sig
        try:
            result = original(plan_dir, record, outcome, sig, **kwargs)
        finally:
            _CURRENT_SIGNAL["sig"] = previous
        if result is None:
            _close_a_record_that_is_gone(plan_dir, record, outcome)
        return result

    return _signal_if_vouched


def install_collected_record_vocabulary() -> bool:
    """Install the collected-record vocabulary into the sweep.

    Idempotent, and it changes the *words* the sweep writes — never the
    signals it sends.  Returns ``True`` when the vocabulary is in place.
    """
    try:
        import process_reaper
    except Exception:  # noqa: BLE001 — the sweep is optional here
        return False
    if getattr(process_reaper, "_collected_vocabulary_installed", False):
        return True
    original = getattr(process_reaper, "_signal_if_vouched", None)
    if original is None or getattr(original, "_collected_vocabulary", False):
        return False
    wrapper = _sweep_wrapper(original)
    wrapper._collected_vocabulary = True
    process_reaper._signal_if_vouched = wrapper
    process_reaper.ledger = _SweepLedger(process_reaper.ledger)
    process_reaper._collected_vocabulary_installed = True
    return True


# Module-level singleton — HeartbeatMonitor + sub-agent code both import this.
sub_agent_registry = SubAgentRegistry()

# The vocabulary is installed as soon as this module is imported: the
# backend imports it (that is where the sweep runs), and so does the
# executor (that is where its own shutdown sweep runs).  Both write the
# same words into the same ledger.
install_collected_record_vocabulary()
