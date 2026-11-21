"""The bounded escalation that ends what the cooperation window could not.

2026-11-20 — stopping a plan's verification work has two halves.  The
cooperative half asks the work to stop on its own: the dispatch
registry is told to shut the plan down, the children are asked to
exit, and the watchdog waits for the plan to go quiet.  What is left
over is the uncooperative half — a child that ignores the request, a
process no layer ever claimed, one that was started after the last
look.  Left alone those keep the plan "busy" for ever, and a watchdog
that waits for ever is a watchdog that never ends.

So this module is the **bounded** end of that wait.  It takes the
survivors the collaboration window could not close, escalates each one
to a ``SIGKILL`` exactly once, runs one orphan sweep over what the
sweep knows about, and reports what is still alive.  Bounded means
bounded: one pass over the survivors, one sweep, one report.  There is
no loop over the survivors and no retry — the escalation is the last
stage, and the report it hands back is the verdict.

Two seams make it usable and testable, and they let the caller own the
two things this module cannot know for itself:

* ``scan`` — what "still alive" means for this plan.  The default is
  :func:`verification_quiesce.scan_plan_processes`, the one place that
  knows both the dispatch registry and the repair executor.  The scan
  is asked once, for the report.
* ``orphan_sweep`` — the extra pass over the processes no layer
  claimed.  The default sweeps the plan's own scan; a caller that
  knows about a ledger the scan cannot see passes its own.

Nothing here raises.  A kill that fails is a kill that failed, not a
crash in the middle of a stop: the report says what is still there and
the caller decides what to do about it.  A stop that raises half way
through leaves more behind than it found.
"""

from __future__ import annotations

import inspect
import logging
import os
import signal
from typing import Any, Callable, Iterable, List, Optional, Set, Tuple

import verification_quiesce
from verification_quiesce import Survivor

logger = logging.getLogger(__name__)

#: The signal the escalation delivers.  There is no gentler stage left:
#: the cooperative stop already asked, and the wait behind this call is
#: bounded.
ESCALATION_SIGNAL = signal.SIGKILL


__all__ = [
    "ESCALATION_SIGNAL",
    "Survivor",
    "escalate",
]


# ---------------------------------------------------------------------------
# The liveness probe
# ---------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    """True when ``pid`` still names a running process.

    The predicate is the reaper's own — the same one the supervisor and
    the quiescence scanner use — so "alive" means one thing in the
    process.  A probe that cannot be answered is answered "no": an
    unreadable pid is not evidence of a live process, and the
    escalation only signals what it can see.
    """
    try:
        import process_reaper

        return bool(process_reaper._alive(pid))
    except Exception:  # noqa: BLE001 — an unreadable pid is not a live one
        logger.warning(
            "[stop_escalation] liveness probe failed pid=%s",
            pid, exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# The signals
# ---------------------------------------------------------------------------


def _own_pgid() -> int:
    """The caller's process group, or ``-1`` when it cannot be read."""
    try:
        return os.getpgrp()
    except (AttributeError, OSError):
        return -1


def _kill_group(pid: int) -> bool:
    """SIGKILL the process group ``pid`` belongs to.

    The group is signalled rather than the process because a survivor
    is usually a *tree*: the child that ignores the stop request often
    has children of its own, and killing the leader alone leaves them
    running.

    Two groups are refused outright.  ``killpg(1, ...)`` is
    ``kill(-1, ...)`` — a broadcast to every process the user owns —
    and the caller's own group would take the caller with it.  Both
    refusals fall back to signalling the process itself, which is what
    the caller wanted anyway: the survivor is one process.
    """
    try:
        pgid = os.getpgid(pid)
    except (AttributeError, OSError):
        return False

    if pgid <= 1:
        logger.warning(
            "[stop_escalation] refusing to signal group %s for pid=%s",
            pgid, pid,
        )
        return False

    if pgid == _own_pgid():
        logger.warning(
            "[stop_escalation] refusing to signal our own group %s "
            "for pid=%s", pgid, pid,
        )
        return False

    try:
        os.killpg(pgid, ESCALATION_SIGNAL)
        return True
    except OSError:
        logger.warning(
            "[stop_escalation] group signal failed pgid=%s pid=%s",
            pgid, pid, exc_info=True,
        )
        return False


def _kill_process(pid: int) -> bool:
    """SIGKILL one process.  Never raises."""
    try:
        os.kill(pid, ESCALATION_SIGNAL)
        return True
    except ProcessLookupError:
        logger.info(
            "[stop_escalation] pid was already gone pid=%s", pid,
        )
        return False
    except OSError:
        logger.warning(
            "[stop_escalation] process signal failed pid=%s",
            pid, exc_info=True,
        )
        return False


def _sigkill(pid: int) -> bool:
    """The one signal this escalation delivers to one process.

    The group goes first; the process itself is the fallback when the
    group cannot be signalled.  Either way the survivor gets **one**
    escalation signal — a second one would be a second chance for a
    process that already ignored everything softer.
    """
    if _kill_group(pid):
        return True
    return _kill_process(pid)


# ---------------------------------------------------------------------------
# The survivors
# ---------------------------------------------------------------------------


def _entry_pid(entry: Any) -> Optional[int]:
    """The pid a survivor carries, or ``None`` when it carries none."""
    try:
        return int(getattr(entry, "pid", 0) or 0)
    except (TypeError, ValueError):
        return None


def _normalise(survivors: Iterable[Survivor]) -> List[Survivor]:
    """The survivors worth escalating, in order, without duplicates.

    A malformed entry — no pid, a pid that is not a number, a pid that
    names nothing (``0``, ``1``, a negative one) — is dropped rather
    than raised: the escalation reports what is left, and one broken
    entry must not cost the whole pass.  The same pid twice is one
    process, so it is escalated once.
    """
    try:
        entries = list(survivors or [])
    except TypeError:
        return []

    targets: List[Survivor] = []
    seen: Set[int] = set()
    for entry in entries:
        pid = _entry_pid(entry)
        if pid is None or pid <= 1:
            continue
        if pid in seen:
            continue
        seen.add(pid)
        targets.append(entry)
    return targets


def _escalate_one(survivor: Survivor) -> bool:
    """End one survivor.  Returns whether it was signalled."""
    pid = _entry_pid(survivor) or 0
    if pid <= 1:
        logger.warning(
            "[stop_escalation] skipping an unknown pid=%s", pid,
        )
        return False
    if pid == os.getpid():
        logger.warning(
            "[stop_escalation] skipping our own pid=%s", pid,
        )
        return False
    if not _alive(pid):
        logger.info(
            "[stop_escalation] pid is already gone pid=%s", pid,
        )
        return False
    return _sigkill(pid)


# ---------------------------------------------------------------------------
# The orphan sweep
# ---------------------------------------------------------------------------


def _positional_arity(sweep: Callable[..., Any]) -> Optional[int]:
    """How many positional arguments ``sweep`` accepts, if readable.

    ``None`` when the callable cannot be introspected — a builtin, for
    instance — and the caller then falls back to the usual shape.
    """
    try:
        signature = inspect.signature(sweep)
    except (TypeError, ValueError):
        return None

    count = 0
    for parameter in signature.parameters.values():
        if parameter.kind in (
            parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD,
        ):
            count += 1
        elif parameter.kind == parameter.VAR_POSITIONAL:
            return 2
    return count


def _sweep_args(
    sweep: Callable[..., Any], plan_id: str, survivors: List[Survivor],
) -> Tuple[Any, ...]:
    """The arguments the sweep is called with.

    A sweep is a seam, and a caller can write it either way — with the
    plan alone, or with the plan and the survivors it is meant to
    finish off.  The arity is read rather than assumed so that both
    work without the caller having to know how this module calls it.
    """
    arity = _positional_arity(sweep)
    if arity is None:
        return (plan_id,)
    if arity <= 0:
        return ()
    if arity == 1:
        return (plan_id,)
    return (plan_id, list(survivors))


def _default_sweep(
    plan_id: str, survivors: List[Survivor], handled: Set[int],
) -> None:
    """The plan's own orphan sweep.

    The default sweep looks at the plan once more and ends what the
    first pass did not name: the processes no layer claimed are exactly
    the ones the survivor list did not mention.  The pids the first
    pass already signalled are left alone — one escalation signal per
    survivor is the rule, and a second one would be a second chance
    for a process that already ignored everything softer.

    The sweep reads the plan through the module's own scanner rather
    than through the caller's, so the caller's scan is asked once, for
    the report.
    """
    try:
        found = list(
            verification_quiesce.scan_plan_processes(plan_id) or []
        )
    except Exception:  # noqa: BLE001 — a sweep that cannot look is no-op
        logger.warning(
            "[stop_escalation] orphan scan failed plan=%s",
            plan_id, exc_info=True,
        )
        return

    for survivor in found:
        pid = _entry_pid(survivor)
        if pid is None or pid in handled:
            continue
        _escalate_one(survivor)


def _run_sweep(
    plan_id: str,
    survivors: List[Survivor],
    sweep: Optional[Callable[..., Any]],
    handled: Set[int],
) -> None:
    """Run the orphan sweep — once, after the kills.

    A sweep that fails is logged and the escalation carries on: the
    sweep is an escalation, not the verdict.
    """
    if sweep is None:
        try:
            _default_sweep(plan_id, survivors, handled)
        except Exception:  # noqa: BLE001 — a failed sweep is not a verdict
            logger.exception(
                "[stop_escalation] orphan sweep failed plan=%s", plan_id,
            )
        return

    try:
        sweep(*_sweep_args(sweep, plan_id, survivors))
    except Exception:  # noqa: BLE001 — a failed sweep is not a verdict
        logger.exception(
            "[stop_escalation] orphan sweep failed plan=%s", plan_id,
        )


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _remaining(
    plan_id: str, scan: Optional[Callable[[str], List[Survivor]]],
) -> List[Survivor]:
    """What is still alive for this plan.

    The scan is the report's view: the default one knows the dispatch
    registry and the repair executor, and a caller that knows more
    passes its own.  A scan that cannot be answered is an empty report
    — the escalation reports what it could see, and never raises.
    """
    scanner = (
        scan if scan is not None
        else verification_quiesce.scan_plan_processes
    )
    try:
        found = scanner(plan_id)
    except Exception:  # noqa: BLE001 — an unreadable plan is an empty one
        logger.warning(
            "[stop_escalation] scan failed plan=%s", plan_id, exc_info=True,
        )
        return []

    if not found:
        return []
    try:
        return list(found)
    except TypeError:
        return []


# ---------------------------------------------------------------------------
# The escalation
# ---------------------------------------------------------------------------


def escalate(
    plan_id: str,
    survivors: Iterable[Survivor],
    *,
    scan: Optional[Callable[[str], List[Survivor]]] = None,
    orphan_sweep: Optional[Callable[..., Any]] = None,
) -> List[Survivor]:
    """End what the cooperation window could not, and report what is left.

    ``survivors`` are the processes the collaboration window could not
    close.  Each one is escalated to a ``SIGKILL`` exactly once — the
    group first, the process itself when the group cannot be
    signalled — and a pid that is unknown or already gone is skipped.

    ``orphan_sweep`` runs once, after the kills: it is the last look
    over the processes no layer claimed.  ``scan`` is asked once, after
    the sweep, and what it finds is the return value — the escalation
    reports what is still alive rather than assuming the kills worked.

    Returns the survivors that are still alive.  Never raises.
    """
    targets = _normalise(survivors)

    handled: Set[int] = set()
    for survivor in targets:
        pid = _entry_pid(survivor)
        _escalate_one(survivor)
        if pid is not None:
            # The pid is recorded whether or not the signal landed: one
            # escalation signal per survivor is the rule, and the
            # sweep below must not give the same process a second one.
            handled.add(pid)

    _run_sweep(plan_id, targets, orphan_sweep, handled)

    return _remaining(plan_id, scan)
