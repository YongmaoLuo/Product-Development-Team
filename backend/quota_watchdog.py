"""One watchdog for every process asleep on a closed quota window.

Why one, and not one per waiting dispatch
-----------------------------------------
Quota state is a property of the *fleet*, not of a caller. A window is
either open — in which case everyone the pool admits may be served — or
shut, in which case nobody may be served, and the only thing that can
change that is the provider. So a dispatch that has found its scene
unavailable is not carrying private information; it is looking at the
same door as its neighbours, and what it needs is a yes/no answer about
a single global fact.

Which is exactly what a per-dispatch watchdog gets wrong. Each one runs
its own probe on its own cadence, and the quota payload is not a
per-provider read: it is fetched from an endpoint that fans out to
every provider at once. So N dispatches waking on the same tick cost N
of those fan-outs to learn one thing, and the cost is paid again every
probe interval for as long as the window stays shut. The barrier in
:func:`provider_order.load_provider_quota_map` does not fix that — it
serializes the requests and shares nothing, and ``force=True`` skips the
second cache check inside it, so each caller runs the body.

So the merge has to happen here, in front of the reader, and it has to
merge *results* rather than requests: :meth:`QuotaWatchdog.probe` lets
exactly one caller do the read while every other caller asking at the
same moment waits for it and takes its answer. One tick, one fan-out,
whatever the fleet size.

What this watchdog deliberately does not have
---------------------------------------------
An upper bound. A closed window reopens on the provider's schedule, and
the length of that schedule is a fact about someone else's account; a
caller that gives up on a figure of its own inventing reports a failure
for a wait it declared over rather than getting the window back. What
bounds the wait is therefore not here — it is the budget the holder
parks (:class:`coding_tool.WorkClock`), which does not spend while the
process is asleep. The two halves have to agree, so the registry that
watches for hung sub-agents skips a handle whose tool is parked here
(see ``sub_agent_registry.SubAgentRegistry.find_stale``): a process
asleep on a quota window is not stuck, and the watchdog meant to prove
it is gone must not be the one that decides otherwise.

The wait is also an observable event, not a silence. Entering the wait,
every probe that actually goes to the wire, and leaving the wait are all
logged with the providers that are shut and the earliest moment any of
their windows comes back.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

log = logging.getLogger("quota_watchdog")

__all__ = [
    "QuotaWatchdog",
    "earliest_reset",
    "get_shared_quota_watchdog",
    "probe_interval_sec",
    "set_shared_quota_watchdog",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def earliest_reset(
    quota_map: Optional[Mapping[str, Any]],
    blocked: Sequence[str] = (),
) -> Optional[str]:
    """ISO stamp of the soonest window reopening among ``blocked``.

    "Soonest" is the number an operator actually needs: if the whole
    fleet is shut, what matters is the first moment any of it can serve
    again, because that is when the wait could plausibly end. Returns
    ``None`` when no shut provider named a moment at all, which is a
    real answer rather than a gap — the reading did not say, and a plan
    upgrade does not print a ``resets_at`` in advance either.
    """
    if not quota_map:
        return None
    soonest: Optional[datetime] = None
    for name in blocked or tuple(quota_map.keys()):
        entry = quota_map.get(name)
        stamp = getattr(entry, "exhausted_resets_at", None)
        if stamp is None or not isinstance(stamp, datetime):
            continue
        if soonest is None or stamp < soonest:
            soonest = stamp
    return soonest.isoformat() if soonest is not None else None


def _blocked_names(quota_map: Optional[Mapping[str, Any]]) -> List[str]:
    if not quota_map:
        return []
    return sorted(
        name for name, entry in quota_map.items()
        if getattr(entry, "exhausted", False)
    )


def _default_fetch() -> Dict[str, Any]:
    """One forced read of the quota payload.

    The import is inside the body so the reader stays a seam: the
    dispatch path resolves it the same way, at call time, which is what
    lets a test substitute the payload instead of standing up a server.
    """
    from provider_order import load_provider_quota_map

    return load_provider_quota_map(force=True)


def _default_interval() -> float:
    try:
        from dynamic_provider_concurrency import DEFAULT_COOLDOWN_SEC
    except Exception:  # pragma: no cover - import cycle guard
        return 300.0
    return float(DEFAULT_COOLDOWN_SEC)


# ---------------------------------------------------------------------------
# The watchdog
# ---------------------------------------------------------------------------


class QuotaWatchdog:
    """The single place a closed quota window is re-read for this process.

    Not a scheduler and not a thread: it owns no background loop and no
    timer. What it owns is the *answer*, and the guarantee that asking
    for it twice at the same moment costs one request rather than two.
    Callers do their own waiting — the dispatch path sleeps on its probe
    cadence, the verification concurrency gate waits on its own bound —
    and this object only answers.

    Thread-safe. Callers are the per-VP dispatch workers plus the
    verification gate, so the same instance is entered from several
    threads and from an event loop at once.
    """

    def __init__(
        self,
        fetch: Optional[Callable[[], Mapping[str, Any]]] = None,
    ) -> None:
        self._fetch = fetch or _default_fetch
        self._condition = threading.Condition()
        self._reading: Dict[str, Any] = {}
        self._fetching = False
        self._probes = 0
        self._shared = 0
        self._waiters = 0
        self._wait_rounds = 0

    # ----- the answer ------------------------------------------------

    def probe(
        self,
        *,
        blocked: Sequence[str] = (),
        scene: str = "",
        logger: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Return a fresh quota reading, sharing one request among callers.

        Exactly one caller runs the reader. Every caller that asks while
        that read is in flight waits for it and receives the same
        payload — which is not a compromise, because the payload answers
        the same question for all of them: the endpoint fans out to
        every provider at once, so a second request cannot report a
        window the first one did not.

        A caller that arrives after the read finished asks again. There
        is deliberately no time-based cache here: the whole point of a
        probe is to learn what changed while the caller was not looking,
        and a remembered answer cannot. What collapses a burst into one
        request is the in-flight merge, not a stale verdict handed round
        the fleet.

        Never raises. A read that blows up is reported as the empty
        mapping, which is what the reader itself means by "no reading":
        every provider stays selectable, and the next caller re-asks
        rather than being served a remembered failure.
        """
        with self._condition:
            if self._fetching:
                shared = True
                self._condition.wait_for(lambda: not self._fetching)
                self._shared += 1
                reading: Optional[Dict[str, Any]] = dict(self._reading)
            else:
                shared = False
                self._fetching = True
                reading = None

        if reading is None:
            try:
                try:
                    fetched = self._fetch() or {}
                except Exception:  # noqa: BLE001 — a failed probe is not a failure
                    log.warning(
                        "the quota watchdog could not read the provider "
                        "quota payload; every provider stays selectable and "
                        "the next probe asks again",
                        exc_info=True,
                    )
                    fetched = {}
                reading = dict(fetched)
            finally:
                with self._condition:
                    self._reading = reading
                    self._fetching = False
                    self._probes += 1
                    self._condition.notify_all()

        self._log_probe(
            logger=logger,
            scene=scene,
            blocked=blocked,
            reading=reading,
            shared=shared,
        )
        return reading

    def _log_probe(
        self,
        *,
        logger: Optional[Any],
        scene: str,
        blocked: Sequence[str],
        reading: Mapping[str, Any],
        shared: bool,
    ) -> None:
        """Make one probe legible in the log, whatever asked for it.

        ``shared`` is the number the merge is for: a probe that cost a
        request says so, and a probe that rode on someone else's says
        that too, so the cost of a closed window can be read off the log
        without instrumenting the reader.
        """
        target = logger if logger is not None else log
        names = sorted(blocked) or _blocked_names(reading)
        try:
            target.info(
                "provider_quota_window_probe",
                f"Probed the provider quota payload for "
                f"{scene or 'the fleet'}"
                f"{' (joined a probe already in flight)' if shared else ''}"
                f"; {len(names)} provider(s) shut",
                data={
                    "scene": scene,
                    "quota_exhausted": names,
                    "earliest_resets_at": earliest_reset(reading, names),
                    "shared": shared,
                    "waiting_dispatches": self.waiting_dispatches,
                    "probes": self.probe_count,
                },
            )
        except Exception:
            # A logger that refuses the event-style signature must not
            # cost the caller its reading.
            pass

    # ----- the wait --------------------------------------------------

    def wait_entered(
        self,
        *,
        scene: str = "",
        blocked: Sequence[str] = (),
        quota_map: Optional[Mapping[str, Any]] = None,
        logger: Optional[Any] = None,
    ) -> None:
        """Record that a dispatch has begun waiting on a closed window.

        The counter is what makes "keep probing while anything sleeps" a
        checkable statement rather than an intention: it is what a caller
        reads to know the wait is still live, and it is what the log
        carries, so an operator can see how much of the fleet is asleep
        rather than inferring it from the absence of output.
        """
        with self._condition:
            self._waiters += 1
            waiters = self._waiters
        target = logger if logger is not None else log
        names = sorted(blocked)
        soonest = earliest_reset(quota_map, names)
        try:
            target.info(
                "provider_quota_window_wait_entered",
                f"{scene or 'a dispatch'} is waiting on "
                f"{len(names)} closed quota window(s)"
                + (f"; the earliest opens at {soonest}" if soonest else ""),
                data={
                    "scene": scene,
                    "quota_exhausted": names,
                    "earliest_resets_at": soonest,
                    "waiting_dispatches": waiters,
                },
            )
        except Exception:
            pass

    def wait_round(self) -> int:
        """Count one probe round and return the running total."""
        with self._condition:
            self._wait_rounds += 1
            return self._wait_rounds

    def wait_left(
        self,
        *,
        scene: str = "",
        rounds: int = 0,
        logger: Optional[Any] = None,
    ) -> None:
        """Record that a dispatch has stopped waiting.

        The wait's end is as worth a log line as its start: a window shut
        when the dispatch gave up and open by the time the next one looks
        is exactly the difference between "quota" and "a provider that
        answers slowly", and only the two ends together say which it was.
        """
        with self._condition:
            self._waiters = max(0, self._waiters - 1)
            waiters = self._waiters
        target = logger if logger is not None else log
        try:
            target.info(
                "provider_quota_window_wait_ended",
                f"{scene or 'a dispatch'} stopped waiting on a closed quota "
                f"window after {rounds} probe round(s)",
                data={
                    "scene": scene,
                    "probe_rounds": rounds,
                    "waiting_dispatches": waiters,
                    "probes": self.probe_count,
                },
            )
        except Exception:
            pass

    # ----- introspection (tests, logs, operators) ---------------------

    @property
    def waiting_dispatches(self) -> int:
        with self._condition:
            return self._waiters

    @property
    def probe_count(self) -> int:
        """Probes that actually reached the reader, for the whole process."""
        with self._condition:
            return self._probes

    @property
    def shared_reads(self) -> int:
        """Probe calls answered by someone else's read rather than their own."""
        with self._condition:
            return self._shared

    def snapshot(self) -> Dict[str, int]:
        with self._condition:
            return {
                "waiting_dispatches": self._waiters,
                "probes": self._probes,
                "shared_reads": self._shared,
                "wait_rounds": self._wait_rounds,
            }


# ---------------------------------------------------------------------------
# Process-wide instance
# ---------------------------------------------------------------------------
#
# One per process, for the same reason the cooldown list and the slot
# tracker are one per process: the fact being watched is global, so a
# second instance would be watching a copy of it.

_lock = threading.Lock()
_instance: Optional[QuotaWatchdog] = None


def get_shared_quota_watchdog() -> QuotaWatchdog:
    """The process-wide watchdog, created on first use."""
    global _instance
    with _lock:
        if _instance is None:
            _instance = QuotaWatchdog()
        return _instance


def set_shared_quota_watchdog(
    watchdog: Optional["QuotaWatchdog"],
) -> Optional["QuotaWatchdog"]:
    """Install (or clear, with ``None``) the process-wide watchdog.

    The escape hatch the cooldown registry has, and for the same reason:
    a test needs a reader it controls. Returns whatever was installed, so
    a caller can put it back.
    """
    global _instance
    with _lock:
        previous = _instance
        _instance = watchdog
        return previous


def probe_interval_sec() -> float:
    """Seconds between two probes of a closed window.

    Re-exported from the same place the dispatch path reads it, so the
    verification gate that shares this watchdog wakes on the same beat as
    a dispatch that is already asleep on one.
    """
    return _default_interval()
