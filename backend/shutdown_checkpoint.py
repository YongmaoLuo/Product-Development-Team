"""The shutdown checkpoint — what the server leaves behind when it goes down.

Why this module exists
----------------------
``server._shutdown_all_executions`` kills the execution and verification
subprocesses and walks away. What it did *not* do was say anything about
the work that was in flight: no file recorded which plan was running,
which task was mid-flight, or which verification round had been reached.
A restart found a plan directory that looked exactly like one that had
never run, and the interrupted work was lost silently.

``pause_state.py`` already knows how to write a per-plan checkpoint.
What was missing was the *caller*: the sweep that, on the way out, looks
at the live execution and verification registries and writes one
checkpoint per plan that was still going. This module is that sweep, and
nothing else — it owns no state of its own beyond the record of which
plans it has already stamped.

The rules it obeys
------------------
Every rule below exists because the shutdown path is the worst possible
place for an exception to be raised: the process is going away, and
whatever it raises takes the rest of the teardown with it.

* **Never raises.** A plan that fails is reported in the result, logged,
  and the sweep moves on to the next plan. The sweep as a whole reports
  ``{'written': [...], 'failed': [...]}`` — never an exception.
* **Per-plan isolation.** One plan's failure is one plan's failure. The
  other plans still get their checkpoints, because a broken plan must
  not cost a healthy plan its resume point.
* **Bounded.** The whole sweep gets :data:`DEFAULT_SHUTDOWN_CHECKPOINT_BUDGET_SECONDS`
  seconds. Past that, the remaining plans are left alone and the sweep
  stops — a shutdown that hangs is worse than a shutdown that loses one
  plan's checkpoint.
* **Idempotent per process.** The lifespan shutdown and the ``atexit``
  hook both call this. A plan already stamped by this process is not
  stamped again, so the second call cannot overwrite the first
  checkpoint's ``paused_at`` with a later one.
* **Honest about what it knows.** A verification record has no
  ``project_dir`` — the runtime state never had one — so the writer
  passes ``None`` rather than inventing a path that would point a
  resume at the wrong place.

What it writes
--------------
For each plan that was in flight::

    PLANS/<plan_id>/pause_state.json
      kind="execution"       # or "verification"
      reason="server_shutdown"
      project_dir="/w/app"   # executions only; null for verifications
      verification_round=3   # verifications only; null for executions
      interrupted_tasks=[...]
      unfinished_tasks=[...]

The interruption lists come from ``routes.execution._stop_interruption``
— the same helper the user-requested ``stop`` uses — so a resume reads
the same shape whether the pause came from a person or from the server
going down.

Reading it back
---------------
:func:`is_shutdown_interrupted` is the other half of the same story: at
startup the backend finds plans whose row still says ``running`` while
their pid is gone, and has to tell "the server went down under this
work" from "the work died".  The checkpoint is the evidence — but only
if it says ``server_shutdown``, is of the kind being asked about, and
is *fresh*: its ``paused_at`` is not earlier than the run's
``started_at``.  A checkpoint left over from an earlier run of the same
plan says nothing about this run, so it is not evidence of a shutdown.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from pause_state import read_pause_checkpoint, write_pause_checkpoint

logger = logging.getLogger(__name__)

#: The ``reason`` every shutdown checkpoint carries. Distinct from the
#: ``user_requested`` a person-requested stop writes, so a resume can
#: tell "the server went away" from "someone pressed stop".
SHUTDOWN_STOP_REASON = "server_shutdown"

#: How long the whole sweep may take before the remaining plans are
#: left alone. The shutdown path is on the critical path of the process
#: going away; a checkpoint is a resume optimisation, not a reason to
#: delay the exit.
DEFAULT_SHUTDOWN_CHECKPOINT_BUDGET_SECONDS = 5.0

#: ``verification_status`` values that mean the verification loop is
#: still cycling. Identical to ``server._VERIFICATION_IN_FLIGHT_STATUSES``
#: — the same three words, no fourth spelling invented here, because a
#: status this module does not recognise is a plan whose checkpoint
#: would say "running" about something that is not.
_VERIFICATION_IN_FLIGHT_STATUSES = ("running", "repairing", "rerunning")

#: Plans this process has already written a shutdown checkpoint for.
#: Module state on purpose: the lifespan shutdown and the ``atexit``
#: hook are two calls in one process, and the second one must not
#: stamp a second checkpoint over the first.
_WRITTEN_PLAN_IDS: Set[str] = set()

#: What a caller may pass as ``interruption_for``: given a plan id and
#: its directory, return ``(interrupted_tasks, unfinished_tasks)``.
InterruptionLookup = Callable[[str, Any], Tuple[Iterable[Any], Iterable[Any]]]


def _default_interruption_for(
    plan_id: str, plan_dir: Any
) -> Tuple[Iterable[Any], Iterable[Any]]:
    """The interruption lists the user-requested ``stop`` would have written.

    Imported lazily and on purpose: ``routes.execution`` imports
    ``server``, and ``server`` is what will import *this* module. A
    module-level import here would close the cycle and make the import
    order decide whether the backend starts at all.

    ``hard_killed=False`` is passed explicitly because the keyword is
    mandatory: the shutdown sweep did not kill a process tree, it killed
    the server that owned one, and the two are different resume
    strategies.
    """
    from routes.execution import _stop_interruption

    return _stop_interruption(plan_id, Path(plan_dir), hard_killed=False)


def _in_flight_plans(
    execution_state: Optional[Dict[str, Any]],
    verification_state: Optional[Dict[str, Any]],
) -> List[Tuple[str, Dict[str, Any], str]]:
    """The plans that were still going, as ``(plan_id, state, kind)``.

    Executions first, then verifications — the order a reader of the
    result expects, and the order that decides which checkpoint wins
    for a plan that somehow appears in both registries.
    """
    targets: List[Tuple[str, Dict[str, Any], str]] = []
    for plan_id, state in (execution_state or {}).items():
        if not isinstance(state, dict):
            continue
        if state.get("status") == "running":
            targets.append((plan_id, state, "execution"))
    for plan_id, state in (verification_state or {}).items():
        if not isinstance(state, dict):
            continue
        if state.get("verification_status") in _VERIFICATION_IN_FLIGHT_STATUSES:
            targets.append((plan_id, state, "verification"))
    return targets


def _failure(plan_id: str, kind: str, exc: BaseException) -> Dict[str, Any]:
    """One entry of the ``failed`` list."""
    return {
        "plan_id": plan_id,
        "kind": kind,
        "error": "%s: %s" % (type(exc).__name__, exc),
    }


def _interruption_lists(
    interruption_for: InterruptionLookup, plan_id: str, plan_dir: Path
) -> Tuple[List[Any], List[Any]]:
    """The interrupted / unfinished tasks, or empty lists if that failed.

    A failure here costs the two task lists and nothing else: the
    checkpoint still says *that* the plan was running when the server
    went down, which is the part a resume cannot do without.
    """
    try:
        interrupted, unfinished = interruption_for(plan_id, plan_dir)
    except Exception:
        logger.exception(
            "shutdown_checkpoint_interruption_failed: cannot read the "
            "interruption lists of plan %s; writing the checkpoint without them",
            plan_id,
        )
        return [], []
    return list(interrupted or []), list(unfinished or [])


def write_shutdown_checkpoints(
    execution_state: Optional[Dict[str, Any]],
    verification_state: Optional[Dict[str, Any]],
    *,
    plan_dir_for: Callable[[str], Any],
    interruption_for: Optional[InterruptionLookup] = None,
    budget_seconds: float = DEFAULT_SHUTDOWN_CHECKPOINT_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    """Write a shutdown checkpoint for every plan that was still running.

    Returns ``{'written': [plan_id, ...], 'failed': [{'plan_id': ...,
    ...}, ...]}``. It never raises: the shutdown path is the last code
    that runs before the process goes away, and an exception here would
    take the rest of the teardown with it.

    ``plan_dir_for`` maps a plan id to its directory. A plan whose
    directory is not there is skipped (and logged) rather than written:
    there is no plan to resume in that case.

    ``interruption_for`` supplies the interrupted / unfinished task
    lists; the default is the same helper the user-requested ``stop``
    uses, so both kinds of pause produce the same resume shape.

    ``budget_seconds`` bounds the whole sweep. ``monotonic`` is the
    clock it is measured against — injectable so the budget can be
    tested without waiting for it.
    """
    written: List[str] = []
    failed: List[Dict[str, Any]] = []
    lookup = interruption_for or _default_interruption_for
    started = monotonic()

    try:
        for plan_id, state, kind in _in_flight_plans(
            execution_state, verification_state
        ):
            elapsed = monotonic() - started
            if elapsed > budget_seconds:
                logger.warning(
                    "shutdown_checkpoint_budget_exhausted: %.3fs of the "
                    "%.3fs budget spent; the remaining plans get no "
                    "checkpoint",
                    elapsed,
                    budget_seconds,
                )
                break

            if plan_id in _WRITTEN_PLAN_IDS:
                # This process already stamped this plan — the atexit
                # hook after the lifespan shutdown, or a second sweep.
                continue

            # Everything below is one plan's work, and everything below
            # is isolated: a failure here costs this plan its
            # checkpoint and nothing else.
            try:
                plan_dir = Path(plan_dir_for(plan_id))
                if not plan_dir.exists():
                    logger.warning(
                        "shutdown_checkpoint_plan_dir_missing: plan %s has no "
                        "directory (%s); skipping its checkpoint",
                        plan_id,
                        plan_dir,
                    )
                    continue

                interrupted, unfinished = _interruption_lists(
                    lookup, plan_id, plan_dir
                )

                project_dir = (
                    state.get("project_dir") if kind == "execution" else None
                )
                verification_round = (
                    state.get("verification_round")
                    if kind == "verification"
                    else None
                )

                write_pause_checkpoint(
                    plan_dir,
                    kind=kind,
                    project_dir=project_dir,
                    reason=SHUTDOWN_STOP_REASON,
                    interrupted_tasks=interrupted,
                    unfinished_tasks=unfinished,
                    verification_round=verification_round,
                )
                if read_pause_checkpoint(plan_dir) is None:
                    # ``write_pause_checkpoint`` swallows a failing disk
                    # write (a checkpoint is an optimisation, not a
                    # reason to fail the stop), so "the call returned"
                    # is not the same claim as "the file is there".
                    # The plan still counts as written -- the sweep's
                    # contract is about the attempt -- but the gap
                    # belongs in the log.
                    logger.warning(
                        "shutdown_checkpoint_not_readable: the checkpoint of "
                        "plan %s did not land in %s",
                        plan_id,
                        plan_dir,
                    )
            except Exception as exc:
                logger.exception(
                    "shutdown_checkpoint_write_failed: cannot write the "
                    "checkpoint of plan %s",
                    plan_id,
                )
                failed.append(_failure(plan_id, kind, exc))
                continue

            _WRITTEN_PLAN_IDS.add(plan_id)
            written.append(plan_id)
    except Exception:
        # The sweep is the last code to run before the process goes
        # away. Whatever went wrong, it goes into the log and the
        # partial result; it does not become an exception on the
        # shutdown path.
        logger.exception(
            "shutdown_checkpoint_aborted: the shutdown checkpoint sweep "
            "stopped early"
        )

    return {"written": written, "failed": failed}


def reset_shutdown_checkpoint_state() -> None:
    """Forget which plans this process has already stamped.

    The set is process state on purpose (the lifespan shutdown and the
    ``atexit`` hook are two calls in one process, and the second must
    not overwrite the first). This is the seam that lets a test — or a
    process that genuinely wants to stamp again — start from scratch.
    """
    _WRITTEN_PLAN_IDS.clear()


def _parse_stamp(value: Any) -> Optional[datetime]:
    """An ISO-8601 stamp as a comparable, timezone-aware datetime.

    ``None`` for anything unusable — that is the caller's cue to treat
    the checkpoint as unproven.

    The two stamps this module compares do not come from the same
    clock: ``pause_state`` writes ``paused_at`` as an aware UTC stamp
    while the execution state writes ``started_at`` as a naive local
    one. :meth:`datetime.astimezone` puts both on the same clock — it
    localises a naive stamp as local time and converts an aware one to
    local time — so the two can be compared without either of them
    throwing on the other.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    try:
        return parsed.astimezone()
    except (OverflowError, OSError, ValueError):
        # A stamp at the very edge of the representable range cannot be
        # localised. It is unusable, not fatal.
        return None


def is_shutdown_interrupted(
    plan_dir: Any,
    *,
    kind: str,
    run_started_at: Optional[str],
    reader: Optional[Callable[[Any], Optional[Dict[str, Any]]]] = None,
) -> bool:
    """Was this plan interrupted by the server going down?

    The question a startup scan has to answer before it decides what to
    do with a plan whose row still says ``running`` while its pid is
    gone: *the server shut down under this work*, or *the work died*?
    The checkpoint is the evidence, and three things have to hold for
    it to count:

    * the checkpoint's ``reason`` is :data:`SHUTDOWN_STOP_REASON` — a
      ``user_requested`` stop is somebody pressing stop, and a
      ``hitl_strict_timeout`` is a third thing again;
    * the checkpoint's ``kind`` is the kind being asked about — a
      verification checkpoint must not wash an execution clean;
    * the checkpoint is *fresh*: its ``paused_at`` is not earlier than
      the run's ``run_started_at``. A checkpoint left over from an
      earlier run of the same plan says nothing about this run.

    Every one of those is a ``False``. The predicate is one-sided on
    purpose: it may only say yes to a shutdown it can actually prove,
    and "cannot prove it" has to mean "treat this as a crash" — the
    conservative direction, because the two verdicts lead to different
    recovery paths and a wrong "shutdown" hides a crash.

    ``run_started_at`` is the run's own start stamp; ``None``, empty
    or unparseable means the freshness cannot be shown, and the answer
    is ``False``.

    ``reader`` is the seam for the checkpoint source; it defaults to
    :func:`pause_state.read_pause_checkpoint` and is called with the
    plan directory.

    Never raises: this runs on the recovery path, where an exception
    would cost every plan its recovery.
    """
    read = reader or read_pause_checkpoint
    try:
        checkpoint = read(plan_dir)
    except Exception:
        logger.exception(
            "shutdown_checkpoint_read_failed: cannot read the checkpoint "
            "of %s; treating the plan as crashed",
            plan_dir,
        )
        return False

    if not isinstance(checkpoint, dict):
        return False
    if checkpoint.get("reason") != SHUTDOWN_STOP_REASON:
        return False
    if checkpoint.get("kind") != kind:
        return False

    paused_at = _parse_stamp(checkpoint.get("paused_at"))
    started_at = _parse_stamp(run_started_at)
    if paused_at is None or started_at is None:
        return False

    return paused_at >= started_at