"""The background supervisor that owns a stop from here to the verdict.

2026-11-20 — a stop that has to *wait* for the plan to fall silent
cannot do that wait on the request thread.  The quiet window is
measured in seconds and the request has to come back in milliseconds,
so the wait moved into a background thread: this one.

The thread owns the two decisions that make a stop honest:

* **when the phase may be flipped** — only after the plan has really
  gone quiet (:func:`verification_quiesce.wait_for_quiescence`), and
* **what to do when it does not** — escalate the sweep, keep watching,
  and at the hard limit report the survivors that are still alive
  instead of claiming the plan stopped.

The state machine is deliberately small:

    ┌─ quiet ─────────────────────────────────► finalize(no survivors)
    │
    ├─ not quiet ─► sweep ─► keep waiting ──┐
    │                                         │
    └───────────────── hard limit ◄──────────┘
              └─► finalize(the survivors)

A plan that never falls silent is *not* finalized early.  It stays in
its stopping phase, and the survivors it still has are what the
callback receives at the hard limit — that report is what lets the
caller say "still running, here is what" instead of "stopped".

How the callback is called
--------------------------

``finalize`` is called in the shape its own signature declares, so a
caller is not forced to wrap a callback it already wrote:

* ``finalize(survivors)`` — the survivors of the stop.  The list is
  empty when the plan went quiet, and carries what was still alive
  when the hard limit was reached.
* ``finalize(plan_id, survivors)`` — both, for a callback that serves
  more than one plan.
* ``finalize(plan_id)`` — the plan id alone.  The value it receives
  *is* the plan id, and the survivors of that stop ride along on it
  as ``.survivors``.

Nothing here raises into the caller: a callback that fails is logged
and the thread still ends.  A supervisor that never exits would be
worse than a phase that did not move.
"""

from __future__ import annotations

import inspect
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import stop_state
import verification_cancel
import verification_quiesce
from verification_quiesce import Survivor

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_HARD_MAX_SECONDS",
    "DEFAULT_MAX_SECONDS",
    "DEFAULT_QUIET_SECONDS",
    "active_plan_ids",
    "start",
]


# ---------------------------------------------------------------------------
# The numbers
# ---------------------------------------------------------------------------

#: Seconds of silence the supervisor waits for before it may flip the
#: phase.  The quiet window is the architecture's: an empty tree has to
#: *stay* empty before it counts.
DEFAULT_QUIET_SECONDS = 5.0

#: Seconds one quiet attempt may take.  When the attempt runs out the
#: plan is still busy, so the sweep escalates and the supervisor keeps
#: watching.
DEFAULT_MAX_SECONDS = 120.0

#: Seconds the whole supervision may take.  At the hard limit the
#: supervisor stops waiting, reports the survivors and finalizes.
DEFAULT_HARD_MAX_SECONDS = 180.0

#: The shortest attempt the supervisor will run — an attempt that is
#: already over is not an attempt.
_MIN_BUDGET = 0.01

#: The longest the supervisor looks at one attempt before giving up on
#: it and escalating.
_MAX_POLL_INTERVAL = 0.5

#: The finest the supervisor looks at the process tree.
_MIN_POLL_INTERVAL = 0.01

#: Grace between the SIGTERM and the SIGKILL of the escalated sweep.
_SIGTERM_GRACE_SECONDS = 3.0

#: How often the sweep checks whether a signalled process is gone.
_SIGTERM_POLL_SECONDS = 0.1

#: How long the test hook waits for a supervisor thread to stop.
_JOIN_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_supervisors: Dict[str, "_Supervisor"] = {}

#: Test seam — the escalated sweep.  ``None`` means "use the default".
#: The default reaches outside the module (it stops dispatches and
#: signals processes), so a test that reaches it installs its own.
_sweep: Optional[Callable[[str, List[Survivor]], None]] = None


class _SupervisionStopped(Exception):
    """The supervisor was asked to stop; the wait is cut short.

    Raised by the scanner wrapper so a stop request ends the quiet
    wait at the next poll instead of at the end of the window.
    """


@dataclass
class _Supervisor:
    """One plan's supervision: the thread, and what it needs to run."""

    plan_id: str
    invoke: Callable[[List[Survivor]], None]
    thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    finalized: bool = False


class _PlanRef(str):
    """The plan id, carrying the survivors of that plan's stop.

    A string, so it is the plan id everywhere a plan id is expected —
    a lookup, a comparison, a log line — and it carries the survivors
    of the stop it belongs to, so a callback that asked only for the
    plan id does not lose the report.
    """

    def __new__(
        cls, plan_id: str, survivors: List[Survivor],
    ) -> "_PlanRef":
        ref = super().__new__(cls, plan_id)
        ref.survivors = list(survivors)
        return ref


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def start(
    plan_id: str,
    *,
    finalize: Callable[..., None],
    scan: Optional[Callable[[str], List[Survivor]]] = None,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    hard_max_seconds: float = DEFAULT_HARD_MAX_SECONDS,
) -> Optional[threading.Thread]:
    """Supervise ``plan_id``'s stop in the background.

    Returns the supervising thread, or ``None`` when there is nothing
    to supervise (an empty plan id).  The thread is a daemon: it never
    keeps the process alive, and it ends on its own once the plan is
    quiet, once the hard limit is reached, or once it is asked to
    stop.

    ``finalize`` is the callback that flips the plan's phase; it is
    called once, with the survivors of the stop (see the module
    docstring for the call shapes it accepts).

    ``scan`` replaces the scanner — the seam that decides what "the
    process tree" means.  It defaults to the plan-scoped scanner of
    :mod:`verification_quiesce`.

    A plan that is already supervised keeps its supervisor: a second
    ``start()`` for the same plan returns the running thread instead
    of starting a second judge of the same silence.
    """
    if not plan_id:
        logger.warning(
            "[stop_supervisor] refusing to supervise an empty plan id",
        )
        return None
    if finalize is None:
        raise ValueError("stop_supervisor.start needs a finalize callback")

    invoke = _bind_finalize(finalize, plan_id)

    with _lock:
        existing = _supervisors.get(plan_id)
        if (
            existing is not None
            and existing.thread is not None
            and existing.thread.is_alive()
        ):
            logger.info(
                "[stop_supervisor] reusing the running supervisor plan=%s",
                plan_id,
            )
            return existing.thread

        supervisor = _Supervisor(plan_id=plan_id, invoke=invoke)
        thread = threading.Thread(
            target=_supervise,
            args=(
                supervisor,
                scan,
                float(quiet_seconds),
                float(max_seconds),
                float(hard_max_seconds),
            ),
            name=f"stop-supervisor:{plan_id}",
            daemon=True,
        )
        supervisor.thread = thread
        _supervisors[plan_id] = supervisor
        # Started under the lock: a second ``start()`` that arrives
        # before the thread is running must still see it as running.
        thread.start()

    return thread


def active_plan_ids() -> List[str]:
    """The plans that currently have a supervisor.

    A plan appears here from the moment its supervision starts and
    disappears when the supervision ends — the quiet verdict, the hard
    limit, or a stop request.
    """
    with _lock:
        return sorted(_supervisors)


# ---------------------------------------------------------------------------
# The supervision
# ---------------------------------------------------------------------------


def _supervise(
    supervisor: _Supervisor,
    scan: Optional[Callable[[str], List[Survivor]]],
    quiet_seconds: float,
    max_seconds: float,
    hard_max_seconds: float,
) -> None:
    """Supervise one plan's stop, from the first scan to the verdict."""
    plan_id = supervisor.plan_id
    baseline = _dispatch_baseline(plan_id)
    scanner = _interruptible_scan(scan, supervisor.stop_event)
    poll_interval = _poll_interval(quiet_seconds)

    deadline = time.monotonic() + hard_max_seconds
    sweep_due = time.monotonic() + max_seconds
    survivors: List[Survivor] = []

    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                logger.warning(
                    "[stop_supervisor] hard limit reached plan=%s survivors=%s",
                    plan_id, len(survivors),
                )
                _finalize(supervisor, survivors)
                return

            if now >= sweep_due:
                # The attempt ran out and the plan is still busy: the
                # sweep escalates, and the supervisor keeps watching.
                _run_sweep(plan_id, survivors)
                sweep_due = time.monotonic() + max_seconds
                continue

            budget = min(sweep_due - now, deadline - now)
            try:
                result = verification_quiesce.wait_for_quiescence(
                    plan_id,
                    quiet_seconds=quiet_seconds,
                    max_seconds=max(budget, _MIN_BUDGET),
                    poll_interval=poll_interval,
                    dispatch_baseline=baseline,
                    scan=scanner,
                )
            except _SupervisionStopped:
                raise
            except Exception:  # noqa: BLE001 — a failed wait is not a verdict
                logger.exception(
                    "[stop_supervisor] quiet wait failed plan=%s", plan_id,
                )
                time.sleep(poll_interval)
                continue

            if result.quiet:
                logger.info(
                    "[stop_supervisor] plan is quiet plan=%s scans=%s",
                    plan_id, result.scans,
                )
                _finalize(supervisor, [])
                return

            survivors = list(result.survivors)
    except _SupervisionStopped:
        logger.info(
            "[stop_supervisor] supervision stopped plan=%s", plan_id,
        )
    finally:
        _end_supervision(supervisor)


def _finalize(supervisor: _Supervisor, survivors: List[Survivor]) -> None:
    """Flip the plan's phase — once, and never on an exception.

    The supervision is over before the callback runs: the plan is no
    longer active, so a caller that waits for the callback and then
    asks which plans are being supervised gets the answer it expects.
    """
    if supervisor.finalized:
        return
    supervisor.finalized = True
    _end_supervision(supervisor)

    try:
        supervisor.invoke(list(survivors))
    except Exception:  # noqa: BLE001 — the thread ends either way
        logger.exception(
            "[stop_supervisor] finalize failed plan=%s", supervisor.plan_id,
        )


def _end_supervision(supervisor: _Supervisor) -> None:
    """Take the plan out of the registry of supervised plans."""
    with _lock:
        if _supervisors.get(supervisor.plan_id) is supervisor:
            _supervisors.pop(supervisor.plan_id, None)


# ---------------------------------------------------------------------------
# The seams
# ---------------------------------------------------------------------------


def _bind_finalize(
    finalize: Callable[..., None], plan_id: str,
) -> Callable[[List[Survivor]], None]:
    """Bind ``finalize`` to the call shape its own signature declares.

    Three shapes are supported, and the callback's signature picks the
    one it gets:

    * ``finalize(survivors)`` — the survivors of the stop;
    * ``finalize(plan_id, survivors)`` — both;
    * ``finalize(plan_id)`` — the plan id, with the survivors riding
      along on it as ``.survivors``.

    A callback that takes ``*args`` is given both.
    """
    try:
        signature = inspect.signature(finalize)
    except (TypeError, ValueError):
        return lambda survivors: finalize(survivors)

    parameters = list(signature.parameters.values())
    positional = [
        p for p in parameters
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    variadic = any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters)

    if variadic or len(positional) >= 2:
        return lambda survivors: finalize(plan_id, survivors)
    if len(positional) == 1:
        name = positional[0].name.lstrip("_").lower().replace("_", "")
        if name in ("plan", "planid"):
            return lambda survivors: finalize(_PlanRef(plan_id, survivors))
        return lambda survivors: finalize(survivors)
    return lambda survivors: finalize()


def _resolve_scan(
    scan: Optional[Callable[[str], List[Survivor]]],
) -> Callable[[str], List[Survivor]]:
    """The scanner to use — the injected one, or the plan-scoped one."""
    if scan is not None:
        return scan
    return verification_quiesce.scan_plan_processes


def _interruptible_scan(
    scan: Optional[Callable[[str], List[Survivor]]],
    stop_event: threading.Event,
) -> Callable[[str], List[Survivor]]:
    """Wrap the scanner so a stop request ends the wait at the next poll.

    The quiet wait polls the scanner; the wrapper turns the stop
    request into an exception on the way through, so the thread leaves
    the wait instead of sitting in it until the window closes.
    """
    resolved = _resolve_scan(scan)

    def _scan(plan_id: str) -> List[Survivor]:
        if stop_event.is_set():
            raise _SupervisionStopped()
        return list(resolved(plan_id) or [])

    return _scan


def _dispatch_baseline(plan_id: str) -> int:
    """The dispatch count the quiet judgement compares against.

    The stop record knows the count from the moment the stop began,
    which is the right reference: "nothing new was dispatched" means
    "nothing since the stop", not "nothing since this thread started".
    A plan with no stop record falls back to the count as it stands.
    """
    try:
        record = stop_state.get(plan_id)
        if record is not None:
            return int(record.dispatch_baseline)
    except Exception:  # noqa: BLE001 — the fallback is good enough
        logger.warning(
            "[stop_supervisor] stop record read failed plan=%s",
            plan_id, exc_info=True,
        )
    return verification_cancel.dispatch_total(plan_id)


def _poll_interval(quiet_seconds: float) -> float:
    """How often the process tree is looked at.

    Finer than the quiet window, so the window is never missed by a
    whole poll, and never finer than it needs to be.
    """
    try:
        window = float(quiet_seconds)
    except (TypeError, ValueError):
        window = DEFAULT_QUIET_SECONDS
    if window <= 0:
        return _MIN_POLL_INTERVAL
    return max(
        _MIN_POLL_INTERVAL, min(_MAX_POLL_INTERVAL, window / 5.0),
    )


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _run_sweep(plan_id: str, survivors: List[Survivor]) -> None:
    """The escalated sweep — the second, harder cleanup pass.

    A sweep that fails is logged and the supervision continues: the
    sweep is an escalation, not the verdict.
    """
    sweep = _sweep or _default_sweep
    try:
        sweep(plan_id, list(survivors))
    except Exception:  # noqa: BLE001 — a failed sweep is not a verdict
        logger.exception(
            "[stop_supervisor] sweep failed plan=%s", plan_id,
        )


def _default_sweep(plan_id: str, survivors: List[Survivor]) -> None:
    """The escalated cleanup pass over what is still alive.

    Two steps, cheapest first: a cooperative stop asks the dispatch
    registry to shut this plan's work down on its own, and only what
    ignores that gets a signal.
    """
    _cooperative_stop(plan_id)
    _signal_survivors(survivors)


def _cooperative_stop(plan_id: str) -> None:
    """Ask the in-process dispatch registry to stop this plan's work.

    The per-plan cooperative stop is optional here: a backend with no
    live dispatch registry has nothing to cooperate with, and that is
    not a failure — the signals below still run.
    """
    try:
        import coding_tool
    except Exception:  # noqa: BLE001 — the signals still run
        logger.info(
            "[stop_supervisor] dispatch registry unavailable plan=%s", plan_id,
        )
        return

    stop = getattr(coding_tool, "stop_live_dispatches_for_plan", None)
    if stop is None:
        return
    try:
        stop(plan_id)
    except Exception:  # noqa: BLE001 — the signals still run
        logger.warning(
            "[stop_supervisor] cooperative stop failed plan=%s",
            plan_id, exc_info=True,
        )


def _signal_survivors(survivors: List[Survivor]) -> None:
    """SIGTERM what is still alive, then SIGKILL what ignores it.

    The escalation is the one the rest of the backend uses: a process
    that ignores the SIGTERM is not left running.
    """
    signalled = [s.pid for s in survivors if _signalable(s.pid)]
    if not signalled:
        return

    for pid in signalled:
        _signal(pid, signal.SIGTERM)

    deadline = time.monotonic() + _SIGTERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not any(_alive(pid) for pid in signalled):
            break
        time.sleep(_SIGTERM_POLL_SECONDS)

    for pid in signalled:
        if _alive(pid):
            _signal(pid, signal.SIGKILL)


def _signalable(pid: int) -> bool:
    """True when ``pid`` is a process the supervisor may signal.

    The supervisor runs inside the server, so the one process it must
    never signal is its own.  Everything else that is still alive and
    was reported as this plan's work is fair game.
    """
    if pid <= 1:
        return False
    if pid == os.getpid():
        return False
    return _alive(pid)


def _signal(pid: int, sig: int) -> bool:
    """Send one signal to one process.  Never raises."""
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def _alive(pid: int) -> bool:
    """True when ``pid`` still names a running process.

    The predicate is the reaper's own — the same one the stop path
    uses — so "alive" means one thing in the process.  A probe that
    cannot be answered is answered "no": an unreadable pid is not
    evidence of a live process.
    """
    try:
        import process_reaper

        return bool(process_reaper._alive(pid))
    except Exception:  # noqa: BLE001 — an unreadable pid is not a live one
        logger.warning(
            "[stop_supervisor] liveness probe failed pid=%s",
            pid, exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# The test hook
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    """Test hook — stop every supervisor and forget them.

    The supervisors are real threads, so the hook does not merely drop
    the registry: it asks each thread to stop and waits for it, so a
    test leaves no thread behind.  A thread that does not stop within
    the grace period is reported and left alone — a stuck thread is
    worth knowing about, and killing it is not this hook's business.
    """
    global _sweep

    with _lock:
        supervisors = list(_supervisors.values())

    for supervisor in supervisors:
        supervisor.stop_event.set()
    for supervisor in supervisors:
        thread = supervisor.thread
        if thread is None:
            continue
        thread.join(timeout=_JOIN_TIMEOUT)
        if thread.is_alive():
            logger.warning(
                "[stop_supervisor] thread did not stop plan=%s",
                supervisor.plan_id,
            )

    with _lock:
        _supervisors.clear()
    _sweep = None
