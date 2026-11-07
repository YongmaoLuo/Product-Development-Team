"""Execution of a terminal round's decision.

Why this module exists
======================
The terminal round of the verification loop stops and asks a human what
happens next.  :mod:`hitl.terminal_action` gave the answers a name
each, so that an answer could be *resolved* to an action.  But a name
is not an execution: nothing reset the round, nothing started it
again, nothing obsoleted a failed point and nothing closed a plan as
failed.  The answer was written down and the backend carried on as if
nobody had been asked.

This module is where an answer becomes a **call**.

Two failure modes it exists to prevent
--------------------------------------

**The chosen option was ignored.**  Every answer used to take the same
path, so the options were indistinguishable downstream.  Here each
action calls exactly the port method it names, and nothing else:
*revise the criterion* obsoletes the failed points and starts the
round again — it does not close the plan, and it does not generate
repair tasks.

**A failed execution lost the answer.**  The answer is already in the
database by the time this runs.  If executing it raised, the human's
answer would be taken down with the work that was about to happen, and
the plan would be left in a state where nobody knows what was decided.
So execution failure **degrades**: the result says ``failed``, the
failure is written to the audit mirror as
``terminal_execution_failed``, and the function returns normally
(arch decision point 5: *read it or degrade, never raise*).

The port
--------

The mechanical work — resetting a round, starting a plan, closing one,
writing repair tasks — lives behind :class:`TerminalExecutionPort`, a
Protocol the caller supplies.  This module therefore names *what* to do
without knowing *how* any of it is done, and the scheduler-dependent
parts (which a later task supplies) can change without touching the
dispatch logic.

Two consequences are worth stating:

* **The core takes no ``engine``.**  Mechanical actions do not depend
  on the scheduler, and the signature is where that is made visible: a
  core that accepted an engine would let the dependency back in
  through the front door.
* **The port is injected, so the dispatch can be tested without a
  running plan.**  The tests in
  ``backend/tests/unit/test_terminal_execution_dispatch.py`` drive it
  with a recording port.

The two entry points
--------------------

:func:`execute_terminal_decision`
    The synchronous core: it runs the action and returns a result
    dictionary (``action`` / ``status`` / ``error``, plus ``count`` for
    generated repair tasks).  It never raises.

:func:`dispatch_terminal_execution`
    The background hand-off: it hands one callable to ``spawn`` (by
    default a daemon thread) and returns immediately, so the caller —
    a route handler that has just recorded an answer — is not held up
    by a plan that takes minutes to restart.

What this module deliberately does NOT do
=========================================

It does not import the orchestrator, the routes or the plan scheduler,
and it does not open a database unless it has to write an audit line.
The sidecar reader (:func:`verification.terminal_decision_session.load`)
is imported lazily, at the point of use, so that importing this module
does not drag the whole verification package in behind it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

from config_paths import resolve_state_db_path
from hitl.store import HitlStore
from hitl.terminal_action import (
    TERMINAL_ACTION_CLOSE_AS_FAILED,
    TERMINAL_ACTION_GENERATE_REPAIR_TASKS,
    TERMINAL_ACTION_RESET_AND_START,
    TERMINAL_ACTION_REVISE_CRITERION,
    TERMINAL_ACTION_UNKNOWN,
    resolve_action,
)

__all__ = [
    "AUDIT_EVENT_FAILED",
    "STATUS_DONE",
    "STATUS_FAILED",
    "TerminalExecutionPort",
    "dispatch_terminal_execution",
    "execute_terminal_decision",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The vocabulary of the result
# ---------------------------------------------------------------------------

#: The action ran to completion.
STATUS_DONE = "done"

#: The action raised.  The failure is audited and the answer is kept.
STATUS_FAILED = "failed"

#: The audit event written when an action raises.
AUDIT_EVENT_FAILED = "terminal_execution_failed"


# ---------------------------------------------------------------------------
# The port
# ---------------------------------------------------------------------------


@runtime_checkable
class TerminalExecutionPort(Protocol):
    """What the executor needs from the world to carry an action out.

    Every method is mechanical and every method takes the plan id, so
    a port can be a thin adapter over the orchestrator, over the plan
    scheduler, or — in a test — a recorder.
    """

    def reset(
        self,
        plan_id: str,
        *,
        clear_artifacts: bool = True,
        clear_root_cause_chain: bool = True,
    ) -> Any:
        """Reset the round's state so it can run again."""
        ...

    def start(self, plan_id: str) -> Any:
        """Start the plan (or the round) running."""
        ...

    def close_as_failed(self, plan_id: str) -> Any:
        """Close the plan as failed — the failure is the accepted outcome."""
        ...

    def generate_repair_tasks(self, plan_id: str, record: Any) -> int:
        """Turn the failed points into repair tasks; return how many."""
        ...

    def revise_criterion(self, plan_id: str, record: Any) -> int:
        """Obsolete the round's failed points; return how many."""
        ...
# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------


def _default_audit(payload: Mapping[str, Any]) -> None:
    """Write one audit line to the HITL audit mirror; never raise.

    The mirror is a file next to the plan, written through
    :class:`~hitl.store.HitlStore`.  A mirror that cannot be written is
    not a reason to fail the caller: the failure being reported is the
    execution failure, and losing it would replace one lost fact with
    another.
    """
    try:
        store = HitlStore(resolve_state_db_path())
        try:
            store.append_audit(payload)
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 - the audit must never raise
        logger.warning("terminal execution audit write failed: %s", exc)


def _audit_failure(
    audit: Callable[[Mapping[str, Any]], Any],
    *,
    plan_id: str,
    decision_id: str,
    action: str,
    error: str,
) -> None:
    """Record exactly one ``terminal_execution_failed`` audit line.

    The call happens once per failure and its own failure is swallowed:
    the caller is owed a result, not a second exception.
    """
    payload: Dict[str, Any] = {
        "event": AUDIT_EVENT_FAILED,
        "decision_id": decision_id,
        "plan_id": plan_id,
        "status": STATUS_FAILED,
        "action": action,
        "note": error,
        "error": error,
    }
    try:
        audit(payload)
    except Exception as exc:  # noqa: BLE001 - the audit must never raise
        logger.warning("terminal execution audit write failed: %s", exc)


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def _resolve_record(
    record: Optional[Mapping[str, Any]], plan_dir: Any
) -> Optional[Mapping[str, Any]]:
    """Return the analysis record to act on.

    An explicit record wins.  Otherwise, when a plan directory is
    given, the sidecar next to the plan is read — and a sidecar that
    cannot be read degrades to ``None`` rather than raising (arch
    decision point 5).  The reader is imported lazily so that this
    module does not pull the verification package in behind it.
    """
    if record is not None:
        return record
    if plan_dir is None:
        return None

    try:
        from verification import terminal_decision_session
    except Exception as exc:  # noqa: BLE001 - a missing reader is no record
        logger.warning("terminal decision session module unavailable: %s", exc)
        return None

    try:
        return terminal_decision_session.load(plan_dir)
    except Exception as exc:  # noqa: BLE001 - an unreadable sidecar is no record
        logger.warning("terminal decision session unreadable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# The core
# ---------------------------------------------------------------------------


def execute_terminal_decision(
    *,
    plan_id: str,
    decision_id: str,
    option: str,
    note: str = "",
    port: TerminalExecutionPort,
    plan_dir: Any = None,
    record: Optional[Mapping[str, Any]] = None,
    audit: Optional[Callable[[Mapping[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    """Carry the action the given answer asks for, and report what happened.

    The answer is resolved through
    :func:`hitl.terminal_action.resolve_action`.  An answer that names
    no option at all is **not** carried out: it is reported as a failed
    decision, audited, and the plan is left alone.  There is no
    interpreter any more — a human who wants something the menu does
    not offer does it outside the framework.

    The result always carries ``action`` (what was asked for),
    ``status`` (``"done"`` or ``"failed"``) and ``error`` (``None``, or
    the failure as it happened).  The *revise the criterion* and
    *generate repair tasks* actions also report ``count`` — how many
    points were obsoleted, how many tasks were generated.

    **This function never raises.**  A step that fails produces a
    ``failed`` result and one audit line; the answer that caused it is
    already recorded and stays recorded.
    """
    action = resolve_action(option)
    audit_fn = audit if audit is not None else _default_audit
    resolved_record: Optional[Mapping[str, Any]] = record

    if action == TERMINAL_ACTION_UNKNOWN:
        # The human wrote something that is not one of the options.
        # Nothing is guessed on their behalf: the decision is recorded
        # as failed, the failure is audited, and the plan is not
        # touched.
        error = "answer is not a terminal option"
        logger.warning(
            "terminal execution cannot run for plan %s (decision %s): %s",
            plan_id,
            decision_id,
            error,
        )
        _audit_failure(
            audit_fn,
            plan_id=plan_id,
            decision_id=decision_id,
            action=action,
            error=error,
        )
        return {"action": action, "status": STATUS_FAILED, "error": error}

    try:
        if port is None:
            raise ValueError("no execution port was supplied")

        resolved_record = _resolve_record(record, plan_dir)

        if action == TERMINAL_ACTION_RESET_AND_START:
            # The chain goes with the reset: a round that restarts with
            # the chain still hanging on it walks straight back into the
            # terminal branch, which is the state the human asked to
            # get out of.
            port.reset(plan_id, clear_root_cause_chain=True)
            port.start(plan_id)
            return {"action": action, "status": STATUS_DONE, "error": None}

        if action == TERMINAL_ACTION_REVISE_CRITERION:
            # The failed points are obsoleted (the criterion they
            # carried is wrong; the new one is written outside the
            # framework), and the round carries on without them.
            count = port.revise_criterion(plan_id, resolved_record)
            port.start(plan_id)
            return {
                "action": action,
                "status": STATUS_DONE,
                "error": None,
                "count": count,
            }

        if action == TERMINAL_ACTION_CLOSE_AS_FAILED:
            # Deliberately terminal: accepting the failure must not
            # start the round again.  Retired from the menu, but a
            # decision recorded before 2026-11-06 still names it.
            port.close_as_failed(plan_id)
            return {"action": action, "status": STATUS_DONE, "error": None}

        if action == TERMINAL_ACTION_GENERATE_REPAIR_TASKS:
            # Retired from the menu, but a decision recorded before
            # 2026-11-06 still names it.
            count = port.generate_repair_tasks(plan_id, resolved_record)
            port.start(plan_id)
            return {
                "action": action,
                "status": STATUS_DONE,
                "error": None,
                "count": count,
            }

        # An action outside the terminal vocabulary is a bug upstream, not
        # something to guess at: it is audited like any other failure.
        raise ValueError(f"unknown terminal action: {action!r}")

    except Exception as exc:  # noqa: BLE001 - execution failure degrades
        error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "terminal execution failed for plan %s (decision %s, action %s): %s",
            plan_id,
            decision_id,
            action,
            error,
        )
        _audit_failure(
            audit_fn,
            plan_id=plan_id,
            decision_id=decision_id,
            action=action,
            error=error,
        )
        return {"action": action, "status": STATUS_FAILED, "error": error}


# ---------------------------------------------------------------------------
# The dispatch
# ---------------------------------------------------------------------------


def _spawn_daemon_thread(target: Callable[[], None]) -> None:
    """Run ``target`` on a daemon thread and return immediately.

    A daemon thread, so a pending execution cannot keep the process
    alive at shutdown.
    """
    thread = threading.Thread(
        target=target, name="terminal-execution", daemon=True
    )
    thread.start()


def dispatch_terminal_execution(
    *,
    plan_id: str,
    decision_id: str,
    option: str,
    note: str = "",
    port: Optional[TerminalExecutionPort] = None,
    plan_dir: Any = None,
    record: Optional[Mapping[str, Any]] = None,
    audit: Optional[Callable[[Mapping[str, Any]], Any]] = None,
    spawn: Optional[Callable[[Callable[[], None]], Any]] = None,
) -> None:
    """Hand the action off to the background and return immediately.

    The caller has just recorded an answer and must not be held up by
    the work that answer asks for: restarting a plan takes minutes, and
    a route handler that waits for it is a route handler that times
    out.  So the action is handed to ``spawn`` — by default a daemon
    thread — and this function returns at once.

    ``spawn`` receives exactly one callable.  Nothing happens until
    that callable is called, which is what makes the hand-off
    observable from a test.

    Whatever the worker does, it never escapes: the core does not
    raise, and the exception handler around it is the last backstop.
    """
    spawn_fn = spawn if spawn is not None else _spawn_daemon_thread

    def _worker() -> None:
        try:
            execute_terminal_decision(
                plan_id=plan_id,
                decision_id=decision_id,
                option=option,
                note=note,
                port=port,  # type: ignore[arg-type]
                plan_dir=plan_dir,
                record=record,
                audit=audit,
            )
        except BaseException:  # noqa: BLE001 - the worker never escapes
            logger.exception(
                "terminal execution worker failed for plan %s (decision %s)",
                plan_id,
                decision_id,
            )

    spawn_fn(_worker)