"""Background verification machinery — the half that owns no route.

Everything here is driven by a thread rather than a request:

  * ``_lazy_check_*`` — the 30s watchdog ticks the ``HeartbeatMonitor`` in
    ``server.py`` fires for execution, verification and sub-agents;
  * the orphan reconcilers that run at startup;
  * :func:`_run_auto_verification_loop` and its repair subprocess plumbing,
    which is what turns a failed round into a repaired one.

It was extracted from ``server.py`` on 2026-09-25. See ``routes/phases.py``
for the late-binding rule that governs every extracted module.
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional, Tuple, Any
from pathlib import Path
from datetime import datetime, timezone
import json
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
import uuid

# 2026-10-29: the base-drift check's own vocabulary.  The round the
# check blocked carries ``stop_reason`` = the check's event name for the
# ``conflicts`` status, so the loop recognises that round by the same
# word the event log uses.  The module is stdlib-only and has no
# import-time side effects, so a top-level import carries no cycle
# risk.
import base_drift
# Late binding into the application module: ``server`` owns the shared
# helpers, request models and module globals, and the suite monkeypatches
# them as ``server.<name>``. Reaching them through the module object —
# rather than importing them by value — is what keeps those patches
# effective. ``server`` seeds ``sys.modules['server']`` before importing
# this module (see the wiring at the bottom of server.py).
import server as _server
# 2026-10-22: the type a cancelled round raises. Dependency-free (stdlib
# only), so a top-level import carries no cycle risk — and the auto-loop
# has to be able to name it in an ``except`` clause, which a lazy
# import inside a hot path could not do cheaply.
from verification_cancel import VerificationCancelled
# 2026-10-23: the single ``project_dir`` validator, shared with the
# start-execution route (see ``backend/project_dir_guard.py``). A
# ``project_dir`` that is not an existing directory is a configuration
# error, and this dispatch must say so *before* it stamps the plan
# ``executing`` and spawns a subprocess that can only fail.
from project_dir_guard import ProjectDirInvalidError, validate_project_dir

# The human-in-the-loop side of a verification round.  Imported by value
# — ``expire_due`` in particular — so that the deadline sweep this
# module runs can be replaced by a test without reaching into
# ``hitl.service`` behind its back.
from config_paths import resolve_state_db_path
from hitl.models import Decision, DecisionMode, DecisionStatus
from hitl.service import expire_due, list_decisions
from hitl.store import HitlStore
from hitl.waiter import wait_for_decision


def _verification_event_logger(plan_dir: Path):
    """The plan's existing structured logger, for the verification coding tool.

    ``create_coding_tool`` takes its logger as an argument and the
    verification call sites never passed one, so ``ClaudeCodingTool.logger``
    was ``None`` for every dispatch the verification path made. Every
    provider-routing event is emitted under ``if self.logger:``, so the whole
    set — the slot queue, the park wait, the closed-window probe, the
    all-pools-exhausted warning, and ``provider_selected`` (the only one
    carrying ``in_flight`` / ``fleet_in_flight``) — was silently dropped on
    exactly the path that needed it. The queue itself still worked; it was
    the record of it that was missing.

    This is not a new logger. It is :func:`execution_logger.get_logger`, the
    same factory the generation phases call, returning the same
    ``ExecutionLogger`` writing the same ``<plans_dir>/<plan_id>/execution.log``
    that ``agent.py`` and ``VerificationAgent`` already write to — so a
    verification dispatch event lands in the log the operator already reads
    for that plan, with no second sink to reconcile.

    ``None`` is a supported answer and means "exactly what happens today":
    the dispatch runs and emits nothing. The ``ExecutionLogger`` constructor
    creates the plan directory, so an unwritable plans directory would
    otherwise turn a logging aid into a dispatch failure. That is why the
    construction is guarded, and why a failure degrades to ``None`` rather
    than propagating.
    """
    try:
        from execution_logger import get_logger

        return get_logger(plan_id=plan_dir.name)
    except Exception:  # noqa: BLE001 — logging must never break a dispatch
        return None


def _count_unfinished_tasks(plan_id: str, tasks_file: Path) -> Dict[str, int]:
    """Cross-check tasks.json against the runtime task_progress overlay.

    Returns counts of tasks whose status cannot be treated as terminal
    success — these are the tasks the executor must still run before
    the plan can advance to the verification round.

    Categories (mutually exclusive):

      * ``pending_or_unstarted`` — task id is in ``tasks.json`` but the
        runtime overlay has no entry for it (never started), or its
        entry's status is ``pending`` / ``in_progress`` /
        ``breakdown_in_progress``. The executor must still resolve
        these tasks.
      * ``failed`` — runtime overlay has ``status="failed"``. These
        are blocker candidates: their downstream tasks are
        perpetually not-ready under the current ``is_dependency_ready``
        contract, so they explain why the executor would otherwise
        exit 0 with leftover pending work.
      * ``total`` — total task ids present in ``tasks.json``.

    The function is defensive: any I/O failure (missing file,
    corrupted JSON, missing SQLite column) returns zeros so the
    caller can still make progress. A ``total == 0`` result means
    "could not read tasks.json" and the caller should treat that as
    "no unfinished tasks detected" — a plan with no readable tasks
    has nothing to fail to run.
    """
    out = {"total": 0, "pending_or_unstarted": 0, "failed": 0}

    # Static task ids from tasks.json, plus each row's on-disk status.
    # ``save_tasks`` strips runtime fields, so the status of a row that was
    # never mirrored into the overlay is only readable here.
    static_ids: set[str] = set()
    static_status: Dict[str, str] = {}
    try:
        data = json.loads(tasks_file.read_text())
        tasks = data if isinstance(data, list) else data.get("tasks", [])
        for t in tasks:
            if isinstance(t, dict) and t.get("id"):
                tid = str(t["id"])
                static_ids.add(tid)
                static_status[tid] = str(t.get("status") or "").lower()
    except Exception:
        return out
    out["total"] = len(static_ids)
    if not static_ids:
        return out

    # Runtime overlay from SQLite plan_execution.task_progress.tasks.
    runtime: Dict[str, Dict[str, Any]] = {}
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )
        conn = open_db(_server._state_db_path())
        try:
            migrate(conn)
            runtime = PlanTaskRepository(conn).load_all(plan_id) or {}
        finally:
            conn.close()
    except Exception:
        # State machine not on disk yet — treat every static task as
        # pending (matches the pre-#3.8 behaviour).
        runtime = {}

    TERMINAL_SUCCESS = {"completed", "skipped"}
    # 2026-10-16: statuses that are RESOLVED but not successful. A
    # split parent now stays in ``tasks.json`` with ``status="superseded"``
    # (its work moved to its children) and a revoked task carries
    # ``"voided"``. Neither is unfinished work, and counting either as
    # ``pending_or_unstarted`` would report a plan that can never finish:
    # the executor would not re-run them (both are terminal for the
    # dispatcher) yet this counter would keep reporting leftover work, so
    # the plan would never advance to its verification round. They join
    # ``failed`` as "not going to run again, and the executor must not sit
    # on them" — but unlike ``failed`` they are not blocker candidates,
    # because ``is_dependency_ready`` follows the superseded pointer and
    # the void cascade has already taken the voided subtree.
    TERMINAL_RESOLVED = {"superseded", "voided"}
    for tid in static_ids:
        entry = runtime.get(tid) if isinstance(runtime.get(tid), dict) else None
        if entry is None:
            # Never written to runtime overlay — the executor never
            # picked this task up.
            #
            # 2026-10-16: a row whose on-disk STATUS says it is resolved
            # is not "never picked up" even when the overlay has no entry.
            # A split parent recorded ``superseded`` can be absent from
            # the overlay, and counting it as unstarted is the same
            # permanent stall (the dispatcher will not re-run it, so no
            # dispatch can ever clear the count).
            if static_status.get(tid, "") in TERMINAL_RESOLVED:
                continue
            out["pending_or_unstarted"] += 1
            continue
        status = entry.get("status")
        if status in TERMINAL_SUCCESS:
            continue
        if status in TERMINAL_RESOLVED:
            continue
        if status == "failed":
            out["failed"] += 1
        else:
            # pending / in_progress / breakdown_in_progress / unknown
            out["pending_or_unstarted"] += 1

    # Overlay-only FAILURES (2026-09-14).
    #
    # A task the runtime recorded as ``failed`` but which is no longer
    # present in ``tasks.json`` used to be invisible here — the loop
    # above only ever looks *outward* from ``static_ids``. That is how a
    # plan gets closed as finished with live failures on the books: its
    # ``tasks.json`` is rewritten
    # by a later refiner pass (every entry ``completed``)
    # while the overlay still carries rows the pass no longer knows
    # about, as
    # ``failed``. ``failed`` comes back 0, the dead-end disambiguation
    # reads that as "no unfinished work", and the chain is terminated
    # over real failures.
    #
    # A bare "count every overlay-only row" would be wrong in the other
    # direction: the refiner legitimately supersedes tasks, and
    # ``test_stale_runtime_row_for_removed_task_ignored`` pins that a
    # removed task must not retroactively block the plan. The
    # distinction that matters is *why* the id left ``tasks.json``:
    #
    #   * it was BROKEN DOWN — ``tasks.json`` carries children named
    #     ``<id>-*``, so the work was redistributed and the parent's
    #     stale failure is not unfinished work (``40-1`` →
    #     ``40-1-1`` / ``40-1-2`` / ``40-1-3``).
    #   * it just vanished — nothing in ``tasks.json`` accounts for it,
    #     so a recorded failure is work the plan still owes (``R1-5``,
    #     whose only definition lives on in
    #     ``verification_tasks_round_1.json``).
    #
    # Only the second shape counts. Non-failed overlay-only rows stay
    # ignored, exactly as before.
    for tid, entry in runtime.items():
        if tid in static_ids or not isinstance(entry, dict):
            continue
        if entry.get("status") != "failed":
            continue
        if any(sid.startswith(f"{tid}-") for sid in static_ids):
            continue  # superseded by its breakdown children
        out["failed"] += 1
    return out


def _are_all_unfinished_blocked_by_failed_upstream(
    plan_id: str,
    unfinished_ids: List[str],
    tasks_file: Path,
) -> bool:
    """Return True iff every unfinished task is blocked by a failed upstream.

    2026-09-11 plan v9 (Bug 2): distinguishes two executor-gave-up
    scenarios:

    (a) ``_all_blocked == True`` — all remaining work is permanently
        blocked because some upstream task failed (tasks deferred by
        an ``upstream_failed:<id>`` verdict). These
        tasks will NEVER succeed; the correct state-machine response
        is "partial completion" → mark them ``skipped`` and enter
        auto-verification on the completed subset.

    (b) ``_all_blocked == False`` — at least one unfinished task has
        no failed upstream (it just wasn't run). This IS a real
        failure; preserve the legacy ``state["status"] = "failed"``
        + ``transition_to("failed")`` behaviour so the operator
        gets a clean signal.

    Algorithm: for each unfinished task id, look up its
    ``depends_on`` list in ``tasks.json``. If any upstream is in the
    runtime overlay with status ``failed``, that task is "blocked".
    If all upstreams are non-failed (completed / pending /
    in_progress / absent / skipped), the task is "genuinely
    unfinished".

    The function is read-only against disk + SQLite; no side effects.
    Returns True only when ALL unfinished tasks are blocked.
    Returns False if any task has no failed upstream, or if
    ``tasks.json`` cannot be parsed (defensive).
    """
    if not unfinished_ids:
        return False
    try:
        data = json.loads(tasks_file.read_text())
    except Exception:
        return False
    tasks = data if isinstance(data, list) else data.get("tasks", [])
    by_id = {
        str(t.get("id")): t
        for t in tasks
        if isinstance(t, dict) and t.get("id") is not None
    }

    # Load the runtime overlay once to check upstream status. We
    # don't need this if no unfinished task has any deps — short
    # circuit when by_id is empty.
    if not by_id:
        # No disk tasks — every unfinished id is an orphan; treat
        # as "no failed upstream" (default → not blocked). Orphans
        # don't have ``depends_on`` on disk so we cannot prove they
        # are blocked; the conservative choice is "fail" rather than
        # silently mark them skipped.
        return False

    runtime: Dict[str, Dict[str, Any]] = {}
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )
        conn = open_db(_server._state_db_path())
        try:
            migrate(conn)
            runtime = PlanTaskRepository(conn).load_all(plan_id) or {}
        finally:
            conn.close()
    except Exception:
        # State machine unavailable — cannot prove blocked; default
        # to "not blocked" so the legacy failed path runs.
        return False

    # Recursive closure — for each unfinished id, walk its
    # ``depends_on`` chain. If any ANCESTOR has ``status == "failed"``
    # in the runtime overlay (or has a failed ancestor of its own),
    # the task is "transitively blocked by failed upstream".
    def _has_failed_ancestor(task_id: str, _seen: set) -> bool:
        """True iff some ancestor of ``task_id`` has ``status ==
        "failed"`` (transitively)."""
        if task_id in _seen:
            return False  # cycle guard
        _seen.add(task_id)
        task = by_id.get(task_id)
        if task is None:
            # Orphan — no static deps on disk, can't trace ancestry.
            return False
        for dep in task.get("depends_on") or []:
            dep_entry = runtime.get(dep)
            if dep_entry is not None:
                dep_status = (dep_entry.get("status") or "").lower()
                if dep_status == "failed":
                    return True
                # 2026-10-16: ``voided`` joins the terminal-not-failed
                # set. A voided ancestor is not a *failure* — nothing
                # failed; the premise was revoked and the cascade has
                # already taken its subtree — so it must not be reported
                # as a blocker. ``superseded`` was already here for the
                # same reason, and the walk stops at either because
                # neither verdict propagates further down.
                if dep_status in ("completed", "skipped", "superseded", "voided"):
                    # Terminal non-failed: walk further to see if a
                    # deeper ancestor failed. This handles cases
                    # like 11-5-1 → [11-4 completed] → [11-2 failed]
                    # where 11-4 is completed but its ancestor 11-2
                    # is the real blocker.
                    if _has_failed_ancestor(dep, _seen):
                        return True
                    continue
                # Dep is in runtime but pending / in_progress /
                # unknown — not a failed blocker. Stop walking this
                # branch (we can't conclude "blocked by failed" via
                # this path; the task is "genuinely unfinished").
                continue
            # Dep not in runtime — fall back to disk ancestry walk.
            if _has_failed_ancestor(dep, _seen):
                return True
        return False

    for tid in unfinished_ids:
        if not _has_failed_ancestor(tid, _seen=set()):
            return False
    return True


def _mark_task_skipped(
    plan_id: str,
    task_id: str,
    reason: str,
) -> bool:
    """CAS ``plan_tasks`` row to ``status='skipped'`` with a reason.

    2026-09-11 plan v9 (Bug 2): used by the partial-completion
    branch to mark blocked-by-failed-upstream tasks as ``skipped``
    so verification knows to exclude them from its scope.

    The write is best-effort. Returns True on successful CAS,
    False on any error (caller decides whether to log). The
    in-memory ``plan_execution.task_progress`` mirror is the
    authoritative view for the current process; the SQLite
    ``plan_tasks`` row is what survives across restarts.

    Idempotent: if the row is already terminal (completed / failed
    / skipped / superseded), the call is a no-op (no version bump).
    """
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )
        conn = open_db(_server._state_db_path())
        try:
            migrate(conn)
            repo = PlanTaskRepository(conn)
            current = repo.load_all(plan_id) or {}
            entry = current.get(task_id)
            if entry is not None:
                existing = (entry.get("status") or "").lower()
                if existing in {"completed", "failed", "skipped", "superseded"}:
                    # Already terminal — don't bump version. This
                    # keeps the partial-completion branch idempotent
                    # so a retry of the same executor doesn't keep
                    # re-writing the same row.
                    return False
            try:
                current_version = repo.get_version(plan_id, task_id)
            except Exception:
                current_version = 0
            repo.update_task(
                plan_id=plan_id,
                task_id=task_id,
                fields={
                    "status": "skipped",
                    "failure_reason": reason,
                    "end_ts": datetime.utcnow().isoformat(),
                },
                expected_version=current_version,
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        # Best-effort: skipped marking is non-fatal; the card will
        # still surface the partial-completion reason via
        # state["stop_reason"]. Log so operator can spot if this
        # keeps failing.
        print(
            f"[mark_task_skipped] failed for plan={plan_id!r} "
            f"task={task_id!r}: {type(exc).__name__}: {exc}",
            file=__import__("sys").stderr,
        )
        return False


def _supersede_prior_round_repairs(
    plan_id: str, round_number: int, vp_ids: Iterable[str],
) -> List[str]:
    """Retire earlier rounds' pending repair tasks for the same VPs.

    2026-10-11 (approved as 4c). A repair batch is regenerated every
    round for the VPs that are still failing. Without this, round N's
    row and round N+1's row for ``VP-007`` both sit in ``plan_tasks``
    as ``pending``, and ``_get_pending_repair_tasks`` — the executor's
    queue — returns both. Over several rounds that piles the same fix up
    once per round for every VP still failing, so most of the queue is
    a re-queue of work already queued.

    Scope is deliberately narrow — same ``failed_vp_id``, strictly
    earlier round, still ``pending``. A repair for a *different* VP is
    untouched even in the same round, and a row that already ran
    (``completed`` / ``failed``) keeps its history. We are not
    retiring "old repair work", we are retiring duplicates of work
    that is about to be re-queued.

    Why ``skipped`` is the right terminal status here (and only here):

    ``skipped`` is a terminal *success* status under
    ``agent.is_dependency_ready`` — it unlocks downstream. That is why
    it must never be used for a task blocked behind a failed upstream
    (the partial-completion branch in ``routes/execution.py`` does that
    wrongly and must not be copied; the repair path deliberately does
    not mark those at all). It IS safe here because the superseded row
    should stop existing: its whole purpose was "VP-007 gets fixed".
    Anything downstream of it is waiting on *that fact*, and the round N+1
    row we just assembled is the thing that will actually deliver it.
    Marking the duplicate ``skipped`` therefore blocks nothing real and
    removes a row that would otherwise re-run the identical fix.

    Returns the list of task ids retired (empty on any failure).
    """
    targets = {str(v) for v in (vp_ids or ()) if v}
    if not targets:
        return []
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )
        conn = open_db(_server._state_db_path())
    except Exception:
        return []
    retired: List[str] = []
    try:
        try:
            migrate(conn)
        except Exception:
            return []
        repo = PlanTaskRepository(conn)
        rows = conn.execute(
            "SELECT task_id, failed_vp_id, round, status FROM plan_tasks "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
        for task_id, failed_vp_id, row_round, status in rows:
            if failed_vp_id not in targets:
                continue
            if str(status or "").lower() != "pending":
                continue
            # Only retire rows from a STRICTLY EARLIER round. Rows in
            # this round (or a later one, which should not exist yet)
            # are left alone.
            try:
                if row_round is None or int(row_round) >= int(round_number):
                    continue
            except (TypeError, ValueError):
                continue
            if _mark_task_skipped(
                plan_id, str(task_id),
                reason=f"superseded_by_round_{round_number}",
            ):
                retired.append(str(task_id))
                _server.logger.info(
                    "[repair_supersede] retired %s (vp=%s round=%s) — "
                    "superseded by round %s",
                    task_id, failed_vp_id, row_round, round_number,
                )
        return retired
    except Exception:
        _server.logger.exception(
            "[repair_supersede] failed plan=%s round=%s", plan_id, round_number,
        )
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_verification_runtime_state(plan_id: str) -> Optional[Dict[str, Any]]:
    """Load persisted verification runtime state from the state-machine repo.

    Returns ``None`` when the state-machine store has no record for
    ``plan_id`` (caller should treat the plan as having no
    in-progress verification).
    """
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.verification_repository import (
            VerificationRepository,
        )
    except ImportError:
        return None
    # 2026-09-23: every exit path must close the connection. This
    # helper is called once per plan directory by
    # :func:`_recover_verification_states` at startup, and none of the
    # four `return` statements below used to close it — so a single
    # boot leaked one SQLite handle (two file descriptors: the database
    # and its WAL) per plan under ``plans/``. The survivors were held
    # for the lifetime of the process by reference cycles that only the
    # cyclic GC ever collected, which is why the count landed between
    # "all of them" and "none of them" and drifted as the GC ran.
    #
    # Its sibling :func:`_recover_execution_states` already does the
    # right thing: open once for the whole scan and reuse. This helper
    # keeps opening per plan because the caller's loop is the natural
    # place for the ``None`` short-circuit, so the fix here is to make
    # the open/close symmetric instead.
    #
    # Regression test: tests/unit/test_recovery_closes_its_connections.py
    conn = None
    try:
        try:
            conn = open_db(_server._state_db_path())
            migrate(conn)
        except (sqlite3.OperationalError, PermissionError, OSError):
            return None
        try:
            repo = VerificationRepository(conn)
            record = repo.current(plan_id)
        except sqlite3.OperationalError:
            return None
    finally:
        if conn is not None:
            conn.close()
    if record is None:
        return None
    runtime = record.get("runtime_state")
    if not runtime:
        return None
    return {
        "verification_status": record.get("verification_status"),
        "verification_round": record.get("round"),
        "verification_max_rounds": record.get("max_rounds"),
        "results": record.get("results"),
        "started_at": record.get("started_at"),
        "updated_at": record.get("updated_at"),
        "verification_stop_reason": record.get("verification_stop_reason"),
    }


def _init_verification_state(
    plan_id: str,
    max_rounds: int,
    saved_state: Optional[Dict[str, Any]] = None,
) -> None:
    """Build ``_verification_state[plan_id]`` in-memory, optionally seeded from disk.

    Single source of truth for the initial-state shape. Both the
    manual ``/start`` endpoint and the auto-verification trigger use
    this helper so the two paths can't drift (e.g. one adding a new
    field and the other forgetting). Callers that want to resume from
    disk pass the loaded ``saved_state``; callers that want a fresh
    start pass ``None``.

    The orchestrator + thread keys are always set to ``None`` — they
    are populated by ``_run`` after the helper returns, never at
    initialisation time.

    Callers should pass the new ``max_rounds`` from the request
    (manual start) or its equivalent (auto-verify) so resume + fresh
    paths can override the saved value consistently.
    """
    if saved_state is not None:
        # Resume path — inherit round / started_at / results, but
        # override the request-controllable bits (plan_id,
        # max_rounds, thread/orchestrator handles).
        _server._verification_state[plan_id] = {
            **saved_state,
            "plan_id": plan_id,
            "verification_max_rounds": max_rounds,
            "orchestrator": None,
            "thread": None,
            "updated_at": datetime.now().isoformat(),
        }
    else:
        # Fresh start — every field is set from the request /
        # current time. ``verification_round`` starts at 0 and the
        # orchestrator increments it as rounds complete. ``thread``
        # and ``orchestrator`` are always initialised to ``None``
        # so the recovery filter (``if state.get("thread")``) and
        # the lazy check both work uniformly across fresh + resume
        # paths.
        _server._verification_state[plan_id] = {
            "plan_id": plan_id,
            "verification_status": "running",
            "verification_round": 0,
            "verification_max_rounds": max_rounds,
            "results": {"pytest_summary": "", "llm_findings": "", "performance_metrics": {}},
            "repair_tasks": [],
            "started_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "orchestrator": None,
            "thread": None,
            "stop_reason": None,
        }


def _requeue_shutdown_interrupted_verification(plan_id: str, plan_dir: Path) -> None:
    """Put a plan whose verification the shutdown interrupted back in the queue.

    The plan's phase is what the scheduler reads, so a plan the server
    took down with it -- still parked at ``verification_running`` (or
    ``verification_repairing``) -- is invisible to the scheduler and to
    every operator looking at the plan list.  Moving it to ``queued``
    says "this is work that is waiting to be picked up again", which is
    what it is: the round is not lost, it is waiting.

    The phase is moved with ``force_set_phase`` because the transition
    tables do not know about this edge (the plan is coming *back* from
    a verification phase, not moving forward inside it) and because a
    plan that crashed mid-round has to be recoverable at all.

    The ``plan_verification`` row is deliberately NOT touched: it is the
    record of the round, and the round is still there -- the checkpoint
    is what a resume reads to pick it up again.

    A failure to write the phase is logged and swallowed: one plan that
    cannot be requeued must not cost every other plan its recovery.
    """
    entry = _server._verification_state.get(plan_id)
    if isinstance(entry, dict):
        entry["resumed_from_shutdown"] = True
    try:
        _server.PlanState(plan_dir).force_set_phase("queued")
    except Exception:
        _server.logger.exception(
            "[verification_recovery] requeue failed plan=%s", plan_id)


def _recover_verification_states(plans_dir: Path) -> None:
    """Restore ``_verification_state[plan_id]`` from disk on startup.

    Only plans with ``verification_status == "running"`` are loaded
    (a terminal-state file is left on disk for audit but the
    in-memory entry is intentionally left empty so the API reflects
    the real state, not stale disk artifacts).

    2026-10-29: a plan whose verification was interrupted by the
    server going down is put back in the queue instead.  The shutdown
    path writes a checkpoint for every plan that was still in flight
    (``backend/shutdown_checkpoint.py``) and the writer covers the three
    in-flight statuses -- ``running``, ``repairing`` and ``rerunning``
    (the same three words as ``server._VERIFICATION_IN_FLIGHT_STATUSES``)
    -- so the reader has to cover the same three.  Without this a plan
    whose round was interrupted stayed at ``verification_running`` for
    ever: the scheduler never saw it, and nothing could pick it up.

    The checkpoint is the *evidence* that the server went down under
    this work -- as opposed to the work having died -- so it is only
    honoured when it is fresh (see
    :func:`shutdown_checkpoint.is_shutdown_interrupted`).  A plan with
    no checkpoint, or with one from an earlier run, keeps the recovery
    it had before: its phase stays where it is, and only a ``running``
    round is loaded into memory.
    """
    if not plans_dir.exists():
        return
    from shutdown_checkpoint import is_shutdown_interrupted

    for plan_dir in plans_dir.iterdir():
        if not plan_dir.is_dir():
            continue
        data = _server._load_verification_runtime_state(plan_dir.name)
        if data is None:
            continue
        status = data.get("verification_status")
        # The same three words the writer side stamps a checkpoint for.
        # A status outside them says nothing about a verification round
        # that was in flight, so it is not a shutdown candidate.
        shutdown_interrupted = (
            status in {"running", "repairing", "rerunning"}
            and is_shutdown_interrupted(
                plan_dir,
                kind="verification",
                run_started_at=data.get("started_at"),
            )
        )
        if shutdown_interrupted:
            _requeue_shutdown_interrupted_verification(plan_dir.name, plan_dir)
            if status != "running":
                # A non-running round never had an in-memory entry, so
                # the requeue is the whole recovery action for it.
                continue
        if status != "running":
            continue
        # In-memory state always carries the orchestrator + thread
        # keys (the start handler creates them), but a recovered plan
        # has neither yet — set to None so the lazy check (which
        # inspects ``state.get("thread")``) skips the dead-thread
        # branch until the user re-calls ``/start``.
        _server._verification_state[plan_dir.name] = {
            **data,
            # 2026-09-14: drop repair_tasks from the INTERRUPTED
            # cycle. The card renders ``_verification_state[*]
            # ["repair_tasks"]`` under the CURRENT round; carrying
            # the old cycle's list into the new run made the card
            # show the previous run's round-4 repair tasks on the
            # new round-1 verification card (cross-round bleed).
            # The round-close handler repopulates the list for the
            # current round; pending RP-* work is NOT lost — the
            # auto-loop reads it from plan_tasks via
            # ``_get_pending_repair_tasks``.
            "repair_tasks": [],
            "orchestrator": None,
            "thread": None,
            # 2026-10-20: mark the entry as RECOVERED. The persisted
            # state says "running", but the process that was running
            # it is gone — this entry was rebuilt from disk at
            # startup. Without the marker the ``/start`` endpoint's
            # "already running" guard reads the status alone and
            # answers 409 forever, so a plan whose backend was killed
            # mid-round could never be started again.
            #
            # The marker is in-memory only: it is never persisted, and
            # the start handler drops it as soon as the round it
            # authorised owns a live thread — from then on the thread
            # itself is the guard.
            "recovered": True,
        }
        if shutdown_interrupted:
            # The requeue above ran before this entry existed, so the
            # helper had no entry to mark. The marker is set here, on
            # the one construction the recovery has always had: it says
            # "this round was resumed after the server went down", which
            # is not the same thing as "this round was recovered from
            # disk because it crashed".
            _server._verification_state[plan_dir.name]["resumed_from_shutdown"] = True
        _server.logger.info(
            "Recovered verification runtime state for plan %s (round=%s, status=%s)",
            plan_dir.name, data.get("verification_round"), data.get("verification_status"),
        )


def _reconcile_orphaned_verification_stages() -> None:
    """CAS routing rows stranded at ``verification_running`` whose
    ``plan_verification`` row is already terminal (2026-09-13 飞书卡片
    split-brain fix).

    Root cause: :func:`_lazy_check_verification` only inspects plans
    present in the in-memory ``_verification_state`` dict.  A plan
    whose verification thread died *across a server restart* leaves
    ``plan_routing.current_phase = 'verification_running'`` on disk with
    ``plan_verification.verification_status`` already terminal.  The
    in-memory dict is empty for it after the restart, so the watchdog
    can never see it again — and every notifier startup sweep
    (``plan_dir_resolver.list_active_plan_ids``) keeps re-pushing the
    plan's failed card (the "卡片一直处于失败状态" report).

    The reconciliation runs once at startup, right after
    :func:`_recover_verification_states`: for every routing row still
    at ``verification_running`` whose
    verification row is terminal (``passed`` / ``failed`` /
    ``loop_stopped``), re-emit :func:`_persist_verification_terminal`
    with ``chain_ending=True``.  That helper is idempotent by
    construction — its step-2 CAS no-ops once the stage has left the
    verification set, and step 1 re-records the same terminal status.

    ``verification_repairing`` is deliberately NOT swept *in general*:
    it is a user-gated pause between rounds, where a terminal
    ``verification_status`` from the *previous* round is expected,
    not orphaned.  The ONE exception (2026-09-14)
    is a dead-ended repairing row — ``verification_stop_reason ==
    'no_repair_tasks'`` — where the repair list is empty and the
    user gate has nothing to confirm.  Those rows are disambiguated
    exactly like the runtime dead end: unfinished execution tasks →
    roll back to ``ready`` (operator resumes explicitly);
    otherwise → chain-ending terminal.  Without this, a plan that
    dead-ended before this fix shipped strands on
    ``verification_repairing`` forever (the 2026-09-04 plan parked
    there with an empty repair list).
    """
    db_path = Path(_server._state_db_path())
    if not db_path.exists():
        return
    from state_machine.db.connection import open as open_db

    try:
        conn = open_db(db_path)
    except Exception as exc:
        _server.logger.warning("[startup_reconcile] open state.db failed: %s", exc)
        return
    try:
        # Deliberately NO ``migrate()`` here: the reconciler must not
        # upgrade a legacy / hand-rolled schema it happens to open
        # (test fixtures and older installs create plan_routing
        # without the workflow-state column — migrating it in place
        # would add NOT NULL columns and break their inserts; red at
        # tests/test_state_from_db.py 2026-09-14).  If the columns /
        # tables the sweep needs are absent, there is simply nothing
        # to reconcile.
        #
        # 2026-09-17 (schema v5): the probe is ``current_phase``, the
        # surviving workflow-state column. Both a v4 database (which
        # has ``stage`` AND ``current_phase``) and a v5 one pass it,
        # which is what we want — the sweep reads ``current_phase``,
        # and ``stage``'s presence or absence is irrelevant to it.
        routing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(plan_routing)")
        }
        if "current_phase" not in routing_cols:
            return
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "plan_verification" not in tables:
            return
        rows = conn.execute(
            "SELECT r.plan_id, v.verification_status, "
            "       v.verification_stop_reason "
            "FROM plan_routing r "
            "JOIN plan_verification v ON v.plan_id = r.plan_id "
            # 2026-10-22: ``verification_rerunning`` left the phase
            # vocabulary (it was a dead state). A row that still holds
            # that historical value reads as ``verification_repairing``
            # — the user-gated phase, which this sweep deliberately
            # does NOT touch.
            "WHERE r.current_phase IN ('verification_running')"
        ).fetchall()
        # Dead-ended user-gated rows: verification_repairing with a
        # no_repair_tasks stop reason has an empty repair list and no
        # confirmation to wait for.
        #
        # 2026-09-14 (live-data correction): the stop reason is NOT
        # reliably in the ``verification_stop_reason`` column — an
        # earlier plan's row has it NULL and carries
        # ``no_repair_tasks`` only inside the ``results`` JSON payload
        # (``{"status": "failed", "stop_reason": "no_repair_tasks",
        # "recorded_by": "_persist_verification_terminal"}``), which is
        # also what the /status endpoint surfaces.  Select the
        # candidates and resolve the reason in Python so the sweep
        # matches the real on-disk shape instead of only the tidy one.
        _verif_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(plan_verification)")
        }
        _has_results_col = "results" in _verif_cols
        _dead_select = (
            "SELECT r.plan_id, v.verification_status, "
            "       v.verification_stop_reason, v.results "
            if _has_results_col
            else "SELECT r.plan_id, v.verification_status, "
                 "       v.verification_stop_reason, NULL "
        )
        dead_rows = conn.execute(
            _dead_select
            + "FROM plan_routing r "
            "JOIN plan_verification v ON v.plan_id = r.plan_id "
            "WHERE r.current_phase = 'verification_repairing'"
        ).fetchall()
        # A repairing row is only dead-ended when nothing is queued for
        # the user to confirm: any pending / in-progress repair task
        # means the gate is doing its job and must be left alone.  The
        # repair-task predicate mirrors ``_get_pending_repair_tasks`` —
        # the canonical ``task_group`` prefix plus both id formats
        # (legacy ``RP-<n>`` and post-v9 ``R<round>-<i>``) — and adds
        # ``in_progress`` so a repair actually being executed by a
        # surviving executor subprocess also keeps the gate intact.
        dead_plan_ids = {r[0] for r in dead_rows}
        pending_repair_plans: set = set()
        if dead_plan_ids and "plan_tasks" in tables:
            pending_repair_plans = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT plan_id FROM plan_tasks "
                    "WHERE status IN ('pending', 'in_progress') "
                    "  AND (task_group LIKE 'repair%' "
                    "       OR task_id LIKE 'RP-%' "
                    "       OR task_id GLOB 'R[0-9]*')"
                )
            }
    except Exception as exc:
        _server.logger.warning("[startup_reconcile] sweep query failed: %s", exc)
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass

    for plan_id, v_status, v_reason, v_results in dead_rows:
        if v_status not in {"failed", "loop_stopped"}:
            continue
        # Resolve the stop reason: the dedicated column first, then the
        # ``results`` JSON payload (the shape the live dashboard reads).
        reason = v_reason
        if not reason and v_results:
            try:
                reason = (json.loads(v_results) or {}).get("stop_reason")
            except (TypeError, ValueError):
                reason = None
        if reason != "no_repair_tasks":
            continue
        if plan_id in pending_repair_plans:
            # Something IS queued for the user to confirm — leave the
            # gate alone.
            continue
        _server.logger.warning(
            "[startup_reconcile] plan=%s dead-ended at "
            "verification_repairing (stop_reason=%s) — disambiguating",
            plan_id, reason,
        )
        _dead_end_terminal(plan_id, reason)
        _server._verification_state.pop(plan_id, None)

    for plan_id, v_status, v_reason in rows:
        if v_status not in {"passed", "failed", "loop_stopped"}:
            # Genuinely still running — leave it to the watchdog.
            continue
        reason = v_reason or "startup_reconciliation_orphaned_stage"
        _server.logger.warning(
            "[startup_reconcile] plan=%s stranded at verification stage "
            "with terminal verification_status=%s stop_reason=%s — "
            "re-emitting terminal persistence",
            plan_id, v_status, reason,
        )
        _server._persist_verification_terminal(plan_id, v_status, reason, chain_ending=True)
        # Drop any in-memory entry recovered from the legacy runtime
        # state file so /status cannot report "running" for a plan
        # whose chain has already ended.
        _server._verification_state.pop(plan_id, None)


#: How many task ids one ``[task_sweep]`` warning line names before it
#: truncates. A plan with a wide fan-out of simultaneously interrupted
#: tasks would otherwise turn a single warning into an unbounded line.
_TASK_SWEEP_LOG_ID_LIMIT = 10


def _reap_orphaned_in_progress_tasks(plan_id: str) -> int:
    """Return a dead plan's orphaned ``in_progress`` tasks to ``pending``.

    Who owns an ``in_progress`` row: the executor subprocess writes it
    right before it dispatches a task. The caller has already
    established that precisely that process is gone — either from the
    boot-time liveness probe in ``_recover_execution_states`` or from
    the runtime watchdog's ``_lazy_check_execution``. So every
    ``status='in_progress'`` row still standing for this plan is
    residue from a run that no longer has an owner.

    Nothing reclaims that residue on its own: no later pass revisits
    ``plan_tasks``, and the progress surfaces read the row directly, so
    the plan keeps reporting "currently running ``<task>``" for as
    long as the row stands, with no process executing anything behind
    it.

    Why ``pending`` rather than ``failed``: the two are the same thing
    to the scheduler. ``agent._TERMINAL_TASK_STATUSES`` excludes only
    ``completed`` / ``failed`` / ``skipped`` / ``superseded``, and
    keeps ``in_progress`` in the DAG as a crash-recovery point — so
    ``_load_tasks`` treats ``pending`` and ``in_progress`` alike and
    this transition changes no scheduling behaviour at all. The only
    thing that changes is whether the plan still claims to be running
    a task.

    ``failed`` would be the opposite of what happened. The task was
    interrupted, never judged, and ``failed`` is terminal: it
    permanently blocks every downstream task on a verdict nothing ever
    reached.

    Only ``in_progress`` rows are touched. Terminal rows
    (``completed`` / ``failed`` / ``skipped`` / ``superseded``) carry
    real verdicts and rows already ``pending`` have nothing to reclaim;
    both are left exactly as they are.

    Idempotent: a second call finds no ``in_progress`` rows and is a
    no-op.

    Never raises. This is the cleanup half of a crash-recovery funnel,
    and a funnel that cannot fail must not be handed a way to fail —
    the caller still owes the remaining recovery side effects.
    Connection handling mirrors ``_get_pending_repair_tasks``: opened,
    migrated, and closed on every path.
    """
    orphans: List[str] = []
    try:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository,
        )

        conn = open_db(_server._state_db_path())
        try:
            migrate(conn)
            repo = PlanTaskRepository(conn)
            orphans = sorted(
                task_id
                for task_id, entry in repo.load_all(plan_id).items()
                if (entry or {}).get("status") == "in_progress"
            )
            for task_id in orphans:
                repo.update_task(plan_id, task_id, {"status": "pending"})
        finally:
            conn.close()
    except Exception:
        # A plan whose state DB is unreadable, or one carrying a task
        # id this repository rejects, must not stop the rest of
        # recovery. Whatever was not reaped stays for the next pass.
        _server.logger.warning(
            "[task_sweep] plan=%s orphaned in_progress task sweep failed",
            plan_id, exc_info=True,
        )
        return 0

    if not orphans:
        return 0
    listed = ", ".join(orphans[:_TASK_SWEEP_LOG_ID_LIMIT])
    if len(orphans) > _TASK_SWEEP_LOG_ID_LIMIT:
        listed += f", +{len(orphans) - _TASK_SWEEP_LOG_ID_LIMIT} more"
    _server.logger.warning(
        "[task_sweep] plan=%s reaped %d orphaned in_progress task(s): %s",
        plan_id, len(orphans), listed,
    )
    return len(orphans)


def _mark_failed_dead(plan_id: str) -> None:
    """Mark an execution as failed because its process is gone.

    Idempotent: only acts if status is still 'running'; only transitions
    plan_state when the current phase is not already terminal. Once the
    idempotency gate has admitted this call, the owning process is
    known to be gone, so the plan's orphaned ``in_progress`` task rows
    are handed back to the scheduler too (see
    :func:`_reap_orphaned_in_progress_tasks`).

    The verdict is written to the row, not only to memory.  The process
    that notices the crash is the next one to go away, and after a
    restart the row is all that is left: a row that carries only
    ``exec_status='failed'`` comes back as a failure with no reason
    (``stop_reason`` NULL), and a row that carries nothing comes back
    as ``running`` — a crash that reads as something else entirely.
    """
    from datetime import datetime
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
        PlanNotFoundError as ExecPlanNotFoundError,
    )

    lock = _server._execution_locks.setdefault(plan_id, threading.Lock())
    with lock:
        state = _server._execution_state.get(plan_id)
        if not state or state.get("status") != "running":
            return
        state["status"] = "failed"
        state["stop_reason"] = "process_died_unexpectedly"
        state["ended_at"] = datetime.now().isoformat()
        # The premise of this whole function is that the executor is
        # gone.  A pid left in the in-memory state belongs to a dead
        # process, and every "is this execution alive?" reader believes
        # a pid means alive — the same ghost the row-level
        # terminal-status rule forbids (accident B, 2026-10-23).
        state["pid"] = None

    try:
        persist_conn = open_db(_server._state_db_path())
        try:
            migrate(persist_conn)
            # The whole verdict goes to the row: the status, the reason
            # and the (now meaningless) pid.  ``update_status`` alone
            # wrote the status and left ``stop_reason`` NULL, so the
            # restart that follows this crash read back "failed, and
            # nobody knows why" — the crash had lost the one field
            # that distinguishes it from any other failure.
            ExecutionRepository(persist_conn).update_fields(
                plan_id,
                exec_status="failed",
                stop_reason="process_died_unexpectedly",
                # The executor is gone; a pid left on a row that says
                # ``failed`` reads as a live execution to every reader.
                exec_pid=None,
            )
        finally:
            persist_conn.close()
    except ExecPlanNotFoundError:
        pass

    # Past this point the idempotency gate has already admitted the
    # call, so the owner is gone and its ``in_progress`` rows are
    # residue. Reap them before the terminal phase write so the plan
    # stops claiming a current task even if the phase write fails.
    _reap_orphaned_in_progress_tasks(plan_id)

    plan_dir = _server._plan_dir(plan_id)
    if plan_dir.exists():
        try:
            ps = _server.PlanState(plan_dir)
            if ps.get_current_phase() not in _server._TERMINAL_PHASES:
                # Recovery context: override normal transition rules.
                # A plan may be in 'ready' if the server crashed between
                # writing execution.json and transitioning to 'executing'.
                ps.force_set_phase("failed")
        except Exception:
            pass


def _update_plan_state_to_terminal(
    plan_id: str,
    stop_reason: str,
    status: str = "failed",
    round_: Optional[int] = None,
) -> None:
    """Side-effect: rewrite ``plan_routing.verification`` so
    ``/api/plan/{id}/summary`` reflects the terminal transition.

    2026-09-19: ``status`` used to be hard-coded ``"failed"``. That was
    fine while the only caller was the watchdog, but it loses the
    verdict for a loop that stopped on ``same_failure_repeated`` /
    ``max_rounds_reached`` (``loop_stopped``) — and this column is what
    ``/api/plan/{id}/summary`` serves as ``state.verification.status``,
    i.e. what the Feishu notifier reads to decide the card header.
    Callers now pass the real verdict; the default keeps the two
    pre-existing call sites unchanged.

    The verification→terminal helpers (``_persist_verification_terminal``
    and the new ``force_terminal`` API) all do the authoritative write
    to ``plan_routing.current_phase`` via SQL CAS, but the summary endpoint
    reads ``state.verification`` from the JSON column on the same row
    (see ``PlanState._sqlite_row_to_state``). Without this side
    effect ``state.verification.status`` would keep showing
    ``running`` while ``plan_routing.current_phase`` is already
    ``terminal_failed`` — and the Feishu notifier (which reads via
    ``/api/plan/{id}/summary``) would render a stale "still verifying"
    card despite the CAS having succeeded.

    2026-09-07: kept the raw-SQL-UPDATE implementation (rather
    than routing through ``PlanState.set_verification_status``) so
    this helper stays in lockstep with the test fixture
    ``tests/test_verification_watchdog_fix.py::TestPlanStateMirror``
    which monkeypatches ``server._state_db_path`` — the previous
    refactor that used ``PlanState._save_state`` ended up calling
    ``plan_state._state_db_path`` (a *separate* function) and
    writing to a different DB than the test fixture seeded/asserted
    against. The current implementation explicitly opens
    ``server._state_db_path()`` so the test's monkeypatch takes
    effect.

    Why not ``PlanState._save_state()`` directly: ``_save_state``
    rebuilds the full row from the in-memory state dict and writes
    ``stage`` from ``_PLAN_PHASE_TO_ROUTING_STAGE[current_phase]``
    (which has no mapping for ``"failed"`` → falls back to
    ``"failed"``). That would overwrite the SQL-CAS'd
    ``stage='terminal_failed'`` we just won with ``terminal_failed
    → failed``, regressing the very transition this function is
    supposed to mirror. Only the ``verification`` column is stale;
    only ``verification`` should be touched.

    Idempotent and best-effort — the routing CAS is the
    authoritative write; this only ensures the verification sub-state
    on the same row catches up. Failures are logged but never raised
    so they don't block the rest of the terminal transition (e.g.
    ``complete_round`` firing ``KIND_PLAN_CLOSED``).
    """
    try:
        import json as _json
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        conn = _open_db(_server._state_db_path())
        try:
            _migrate(conn)
            # Read the existing verification JSON so we preserve
            # ``round`` / ``max_rounds`` rather than clobbering them.
            row = conn.execute(
                "SELECT verification FROM plan_routing WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                _server.logger.warning(
                    "[plan_state_mirror] no plan_routing row for plan=%s; "
                    "skipping verification sub-state update",
                    plan_id,
                )
                return
            existing_raw = row[0]
            try:
                existing_verif = _json.loads(existing_raw) if existing_raw else {}
            except (TypeError, ValueError):
                existing_verif = {}
            new_verif = {
                "status": status,
                # ``round_`` is authoritative when the caller has it
                # (``_persist_verification_terminal`` reads it straight
                # off ``plan_verification``). Falling back to the routing
                # JSON matters because step 3 above writes that column
                # from ``PlanState._state``, which may still hold the
                # pre-round value.
                "round": (
                    round_
                    if round_ is not None
                    else existing_verif.get("round", 0)
                ),
                "max_rounds": existing_verif.get(
                    "max_rounds", _server.DEFAULT_MAX_VERIFICATION_ROUNDS,
                ),
                "stop_reason": stop_reason,
            }
            # ``verification`` is the only column we touch. We do NOT
            # touch ``current_phase`` (already written by the caller's
            # CAS→transition chain) — going through
            # ``PlanState._save_state`` here would risk overwriting that
            # authoritative write.
            #
            # Before schema v5 this comment had to spell out that
            # ``_PLAN_PHASE_TO_ROUTING_STAGE`` had no mapping for
            # ``"failed"``, so a re-derive would write ``stage='failed'``
            # and clobber the caller's ``terminal_failed``. There is no
            # map and no second column now, so that failure mode is gone
            # rather than merely avoided.
            conn.execute(
                "UPDATE plan_routing SET verification = ?, last_updated = ? WHERE plan_id = ?",
                (_json.dumps(new_verif), datetime.now().isoformat(), plan_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        _server.logger.exception(
            "[plan_state_mirror] update failed for plan=%s; "
            "SQLite stage is correct but summary endpoint may show stale verification.status",
            plan_id,
        )


def _mark_verification_failed_dead(plan_id: str) -> None:
    """Mark a verification as failed because its background thread died.

    Mirrors ``_mark_failed_dead`` but for the verification thread. The
    ``_verification_state`` in-memory dict is updated immediately so
    subsequent /api/verification/{id}/status calls return the real state.
    The plan_state transition is best-effort (the daemon thread may have
    died before the transition could run).
    """
    from datetime import datetime

    lock = _server._verification_locks.setdefault(plan_id, threading.Lock())
    with lock:
        state = _server._verification_state.get(plan_id)
        if not state or state.get("verification_status") != "running":
            return
        state["verification_status"] = "failed"
        state["stop_reason"] = "verification_thread_died_unexpectedly"
        state["updated_at"] = datetime.now().isoformat()
        state["ended_at"] = state["updated_at"]
        # 2026-07-19 / 2026-08-25: capture a side-channel snapshot of
        # the in-memory state for persistence. We must write it
        # *outside* this ``with`` block — any helper that re-acquires
        # the same non-reentrant ``_verification_locks[plan_id]``
        # would deadlock if the same thread already held it. The
        # snapshot is taken here so we don't lose the updated
        # ``verification_status``/``stop_reason``/``ended_at`` if the
        # post-lock code raises.
        in_memory_state_snapshot = dict(state)

    # Persist the side-channel snapshot so a server restart (and the
    # next ``POST /api/verification/{id}/start`` call) sees the
    # failure rather than resurrecting the stale "running" verdict.
    plan_dir = _server._plan_dir(plan_id)
    if plan_dir.exists():
        try:
            runtime_path = plan_dir / ".verification_runtime.json"
            runtime_path.write_text(
                json.dumps(in_memory_state_snapshot, default=str),
                encoding="utf-8",
            )
        except Exception:
            _server.logger.exception(
                "verification_runtime_persist_failed plan_id=%s", plan_id,
            )
        # NOTE: do NOT call ``_persist_verification_terminal`` here.
        # ``_lazy_check_verification`` (the only caller) already invokes
        # it on the line after this function returns. Calling it twice
        # would (a) double the ``_persist_verification_terminal`` count
        # which the watchdog dedup test (``TestWatchdogDedup``) asserts
        # is exactly 1 per episode and (b) double-fire
        # ``KIND_PLAN_CLOSED`` on the state bus. The original
        # ``ps.transition_to("verification_failed")`` is preserved as a
        # best-effort plan-level marker; the routing CAS lives in
        # ``_persist_verification_terminal`` step 2 below.
        try:
            ps = _server.PlanState(plan_dir)
            if not ps.get_current_phase().startswith("verification_"):
                ps.transition_to("verification_failed")
        except Exception:
            pass


def _lazy_check_execution(plan_id: str) -> None:
    """Probe a single running execution via os.kill(pid, 0).

    - ProcessLookupError → mark failed (process_died_unexpectedly)
    - PermissionError    → process exists but isn't ours; treat as alive
    - Success            → alive; no state change
    - No PID but running → check tasks.json for progress; if stale, mark failed
    """
    state = _server._execution_state.get(plan_id)
    if not state or state.get("status") != "running":
        return
    pid = state.get("pid")
    if not pid:
        # No PID but status is running — likely recovered from incomplete execution.json
        # Check if there's been any recent progress in tasks.json
        project_dir = _server._get_project_dir(plan_id)
        if project_dir:
            tasks_file = project_dir / "tasks.json"
            if tasks_file.exists():
                try:
                    import time
                    mtime = tasks_file.stat().st_mtime
                    if time.time() - mtime > 300:  # No update in 5 minutes
                        _server._mark_failed_dead(plan_id)
                except OSError:
                    pass
        return
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # 2026-09-11 plan v11 (watchdog race fix):
        # ``_run`` 协程 sets ``executor_finished_cleanly=True`` as
        # soon as ``process.wait()`` returns, before mutating
        # ``state["status"]``. If the PID is gone but that flag is
        # set, the success path is already in flight (writing
        # ``state["status"]="completed"`` + DB persistence +
        # ``_run_auto_verification_loop``). Firing
        # ``_mark_failed_dead`` here would race the success path
        # and leave the state machine at ``failed`` even
        # though the executor returned 0. Yield to the success
        # path and return; the watchdog will see the updated
        # ``status`` on its next tick (or the success path will
        # transition ``plan_routing.current_phase`` to ``completed``
        # which ``_lazy_check_execution`` checks on entry).
        if state.get("executor_finished_cleanly"):
            return
        _server._mark_failed_dead(plan_id)
    except PermissionError:
        pass


def _normalized_vp_token(vp_id: str) -> str:
    """Filesystem/process token for a VP id: ``VP-023`` → ``vp_023``.

    The verification sub-agent prompt convention tee's progress to
    ``/tmp/vp_<id>_progress.log`` and the agents that background their
    pytest keep the lowercased underscore form in junit/tee paths, so
    both /tmp artifact globs and ``pgrep -f`` patterns use this token.
    """
    return re.sub(r"-", "_", str(vp_id)).lower()


def _vp_artifact_processes(vp_id: str) -> List[int]:
    """PIDs whose cmdline references this VP's own /tmp tee/junit artifacts.

    ``pgrep -f`` pattern is ``/tmp/.*<vp_token>`` — deliberately narrow:
    the VP id alone (``VP-023`` / ``vp_023``) is common enough to match
    unrelated processes (an editor buffer, a grep), while a path under
    /tmp carrying the VP token is written by the verification sub-agent's
    own tee/junit convention and nothing else on this machine.

    Returns ``[]`` on any failure (pgrep missing, timeout, no match).
    """
    token = _normalized_vp_token(vp_id)
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"/tmp/.*{token}"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return []
    pids: List[int] = []
    for pid_str in out.stdout.split():
        try:
            pids.append(int(pid_str))
        except ValueError:
            continue
    return pids


def _verification_liveness_probe(
    plan_id: str, running_vp_ids: List[str],
) -> Tuple[bool, str]:
    """Triage probe: is a VP that looks log-stale actually making progress?

    Signals (any one → alive):

      1. a **live process** whose cmdline references the VP's /tmp
         artifacts — strongest evidence, and the only signal that
         survives a legitimately quiet phase (e.g. ``docker compose up``
         blocking for minutes without writing to the tee file);
      2. a ``/tmp`` artifact (tee'd pytest progress, junit xml, agent
         logs) touched within the freshness window;
      3. a ``sub_agent_registry`` handle for this plan whose
         ``last_progress_ts`` — or whose tool's ``_last_output_ts``
         (streaming stdout) — is fresh.

    The freshness window is ``min(staleness threshold, 900s)``: "recent
    output" must mean recently, not "since the last stale check" — with
    a 3600s threshold in ``.env``, using the threshold would call a tee
    file last touched 59 minutes ago fresh.

    Returns ``(alive, detail)``. Never raises — a probe failure is
    treated as "no signal" so the watchdog errs toward its hard cap.
    """
    now = time.time()
    fresh_window = min(_server.VERIFICATION_WATCHDOG_STALENESS_SECONDS, 900.0)
    for vp_id in running_vp_ids:
        pids = _server._vp_artifact_processes(vp_id)
        if pids:
            return True, f"live process {pids[0]} references {vp_id} /tmp artifacts"
        token = _normalized_vp_token(vp_id)
        try:
            for path in _server._VP_TMP_ROOT.glob(f"*{token}*"):
                try:
                    if now - path.stat().st_mtime < fresh_window:
                        return True, f"/tmp artifact {path.name} fresh"
                except OSError:
                    continue
        except Exception:
            pass
    try:
        for handle in _server.sub_agent_registry.all_handles(plan_id):
            try:
                if now - handle.last_progress_ts < fresh_window:
                    return True, f"registry handle {handle.vp_id} progress fresh"
                scoped_tool = getattr(handle, "scoped_tool", None)
                last_output = (
                    getattr(scoped_tool, "_last_output_ts", None)
                    if scoped_tool is not None else None
                )
                if last_output is not None and (
                    time.monotonic() - last_output
                ) < fresh_window:
                    return True, f"registry handle {handle.vp_id} streaming"
            except Exception:
                continue
    except Exception:
        pass
    return False, "no fresh liveness signal"


def _kill_orphaned_vp_processes(running_vp_ids: List[str]) -> List[int]:
    """Kill disowned (PPID-1) pytest/bash orphans the registry can't see.

    Uses :func:`_vp_artifact_processes` for the match (see its docstring
    for why the pattern is the /tmp artifact path and not a bare VP id).
    Killed pids are returned for the audit breadcrumb; never raises.
    """
    killed: List[int] = []
    for vp_id in running_vp_ids:
        for pid in _server._vp_artifact_processes(vp_id):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue
            except Exception:
                continue
            # Brief grace, then SIGKILL escalation.
            for _ in range(10):
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError):
                    break
                time.sleep(0.5)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
            killed.append(pid)
    return killed


def _cleanup_dead_verification_processes(
    plan_id: str, running_vp_ids: List[str],
) -> None:
    """Kill everything left behind by a truly-dead verification.

    Two populations:

      1. Registered sub-agent handles (the LLM tool subprocess) —
         SIGTERM→SIGKILL via the existing ``_kill_sub_agent_process``
         escalation, then unregister so the 30s tick doesn't refire;
      2. Orphaned background pytest/bash (PPID 1, disowned by the
         agent after the Bash 10-min timeout taught it to background
         long runs) — matched by their /tmp artifact cmdline.

    2026-09-14: flagging a verification dead WITHOUT
    killing its processes leaves zombies burning CPU forever (the
    VP-023 pytest orphan outlived its own stale flag by >1h). Kill
    first, then persist the terminal state.
    """
    try:
        for handle in list(_server.sub_agent_registry.all_handles(plan_id)):
            try:
                _server._kill_sub_agent_process(handle)
            except Exception:
                _server.logger.exception(
                    "[verification_watchdog] sub-agent kill failed "
                    "plan=%s vp=%s", plan_id, getattr(handle, "vp_id", "?"),
                )
            try:
                _server.sub_agent_registry.unregister(plan_id, handle)
            except Exception:
                pass
    except Exception:
        _server.logger.exception(
            "[verification_watchdog] registry cleanup failed plan=%s",
            plan_id,
        )
    try:
        killed = _server._kill_orphaned_vp_processes(running_vp_ids)
        if killed:
            _server.logger.warning(
                "[verification_watchdog] killed orphaned vp processes "
                "plan=%s pids=%s", plan_id, killed,
            )
    except Exception:
        _server.logger.exception(
            "[verification_watchdog] orphan sweep failed plan=%s", plan_id,
        )


# ---------------------------------------------------------------------------
# HITL deadlines
# ---------------------------------------------------------------------------
#
# 2026-10-22: a verification round that asks a human a question has to
# live with the answer not coming, and the two kinds of question end
# differently.
#
# An ``auto`` question carries a default answer, so when its deadline
# passes the default becomes the answer and the round carries on.
# ``hitl.service.expire_due`` owns that transition; the tick below is
# what decides *when*.
#
# A ``strict`` question has no substitute for a human, so its default
# answer must never be used — not even when the deadline passes.  What
# happens instead is that the round *stops*: the question becomes
# ``paused`` (still answerable), and the plan gets a
# ``pause_state.json`` naming it, so the resume path can find the
# question again instead of guessing which one it was.
#
# The verification tick is the only thing that runs while a round is
# blocked on a human, so it is where both deadlines are noticed.

#: The reason a pause checkpoint carries when a strict question ran out
#: of time without anybody answering it.
HITL_STRICT_TIMEOUT_REASON = "hitl_strict_timeout"


def _hitl_now() -> datetime:
    """The clock the HITL deadlines are read against.

    A function of its own, so a test can inject a clock instead of
    sleeping through one: a deadline is seconds wide, and a test that
    has to wait for it is a test that flakes.  Production reads the
    wall clock.
    """
    return datetime.now(timezone.utc)


def _expire_hitl_decisions(now: datetime) -> int:
    """Resolve the ``auto`` questions whose deadline has passed.

    ``hitl.service.expire_due`` knows *how* an overdue question
    resolves — an ``auto`` question nobody answered ends ``defaulted``,
    with its stored default as the answer; a ``strict`` one is not
    touched at all.  This knows *when*: the verification tick is a
    periodic entry, and a round that is waiting for a human must not
    keep waiting on a question that already has an answer.

    A failure here is logged and swallowed.  The sweep writes to a
    database another thread may be writing to, and a watchdog that died
    on its first expiry would leave every later question waiting
    forever — silently, because the tick's caller swallows what the
    tick raises.

    Returns how many questions the sweep resolved.
    """
    try:
        return expire_due(now=now)
    except Exception as exc:  # noqa: BLE001 — a failed sweep must not end the tick
        _server.logger.exception(
            "[verification_watchdog] hitl expiry sweep failed: %s", exc,
        )
        return 0


def _handle_hitl_strict_timeout(plan_id: str, now: datetime) -> Optional[str]:
    """Pause the strict questions whose deadline passed; record the pause.

    A ``strict`` question is one the backend may not answer in a
    human's place.  When its deadline passes there is nothing to fall
    back to, so the question cannot end by itself — and it must not be
    ended by its default option either.  What *can* happen is that the
    round stops waiting:

      * the question becomes ``paused`` — it is still open, and a later
        answer is taken (``hitl.service.answer_decision`` moves a
        paused row to ``answered``);
      * the plan gets a ``pause_state.json`` naming that question, so
        the resume path can find it again.

    Every overdue strict question of the plan that is the round's own
    is paused, and the checkpoint names the oldest of them: the pause is
    the round's, and the round was waiting on the question that ran out
    of time first.

    Only the questions the *executor* did not ask are the round's
    — a question the execution side asked is the executor's, and the
    executor's own gate is what holds its work up (see
    :func:`_overdue_strict_decisions`).

    Returns the id of the question that caused the pause, or ``None``
    when nothing was overdue.
    """
    try:
        with HitlStore(resolve_state_db_path()) as store:
            overdue = _overdue_strict_decisions(store, plan_id, now)
            paused = [
                decision.decision_id
                for decision in overdue
                if store.mark_paused(
                    decision.decision_id, note=HITL_STRICT_TIMEOUT_REASON
                )
            ]
    except Exception as exc:  # noqa: BLE001 — a failed sweep must not end the tick
        _server.logger.exception(
            "[verification_watchdog] strict timeout handling failed plan=%s: %s",
            plan_id, exc,
        )
        return None

    if not paused:
        return None

    cause = paused[0]
    _write_hitl_pause_checkpoint(plan_id, cause)
    return cause


def _overdue_strict_decisions(
    store: HitlStore, plan_id: str, now: datetime
) -> List[Decision]:
    """The plan's pending strict questions whose deadline has passed.

    Only ``pending`` rows are considered: a question that has already
    been answered, defaulted or paused is settled, and a settled
    question is nobody's business any more.

    Only questions the *executor* did not ask are considered, for
    the same reason :func:`_open_verification_decisions` waits on
    the verification side's and no others: a question the execution
    side asked belongs to the executor, and the executor's own gate
    (``hitl.service.blocked_task_ids``) is what holds that work up.
    Pausing such a question here would take it out of the ``pending``
    list the operator's to-do reads — the plan would sit on a problem
    nobody can see any more — and the checkpoint written beside it
    would name a question this round never asked about.

    The two sides are the architecture's own (``verification |
    execution``).  A question the executor asked carries
    :data:`EXECUTOR_DECISION_SOURCE`; every other question is the
    round's — the verification side's own questions, and the questions
    asked through the generic route that nobody claimed (the route
    records its own source for a decision *nobody else claimed*).  The
    architecture has no third side, and a question nobody claimed is a
    question the round is answerable for: the plan's strict-expiry
    rule — a ``pause_state.json`` of kind ``verification`` naming the
    question — is what happens to it.
    """
    return [
        decision
        for decision in list_decisions(
            plan_id, DecisionStatus.PENDING.value, store=store
        )
        if decision.source != EXECUTOR_DECISION_SOURCE
        and decision.mode == DecisionMode.STRICT.value
        and _deadline_has_passed(decision, now)
    ]


def _deadline_has_passed(decision: Decision, now: datetime) -> bool:
    """True when the question's deadline has arrived by ``now``.

    The deadline is the *end* of the waiting window, so the window is
    half-open: before it the question is still open, at it the answer
    is late.  A question with no deadline never expires — it waits for
    a human for as long as it takes.
    """
    raw = decision.deadline_at
    if not raw:
        return False
    text = str(raw).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        deadline = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        # A deadline that cannot be read is a deadline that cannot be
        # compared, and an undecidable deadline is not an expired one:
        # the question waits rather than resolving itself on a guess.
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    else:
        deadline = deadline.astimezone(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return deadline <= now


def _write_hitl_pause_checkpoint(plan_id: str, decision_id: str) -> None:
    """Record the strict timeout in the plan's pause checkpoint.

    ``pause_state.write_pause_checkpoint`` owns the document's shape;
    this decides what goes into it.  Three fields carry the meaning:
    the kind (``verification`` — the pause happened on the
    verification side), the reason (``hitl_strict_timeout``) and the
    question that caused it, so the resume path can find that question
    again instead of guessing which one it was.

    The interrupted round is recorded too, so a later start resumes
    *that* round rather than starting a new one.
    """
    from pause_state import write_pause_checkpoint

    plan_dir = _server._plan_dir(plan_id)
    project_dir: Optional[str] = None
    try:
        resolved = _server._get_project_dir(plan_id)
        project_dir = str(resolved) if resolved else None
    except Exception as exc:  # noqa: BLE001 — a checkpoint without a dir is still one
        _server.logger.exception(
            "[verification_watchdog] project dir lookup failed plan=%s: %s",
            plan_id, exc,
        )

    state = _server._verification_state.get(plan_id) or {}
    round_n = state.get("verification_round")
    write_pause_checkpoint(
        plan_dir,
        kind="verification",
        project_dir=project_dir,
        reason=HITL_STRICT_TIMEOUT_REASON,
        interrupted_tasks=[],
        unfinished_tasks=[],
        verification_round=round_n if isinstance(round_n, int) else None,
        decision_id=decision_id,
    )


# ---------------------------------------------------------------------------
# Waiting for a human
# ---------------------------------------------------------------------------
#
# 2026-10-23: a round that asks a human a question has to WAIT for the
# answer — and while it waits it has to look alive.
#
# Two facts made the wait impossible before this:
#
# * ``hitl.waiter`` existed and worked, but nothing called it.  A
#   question asked of a human was asked and then forgotten: the round
#   walked past it and carried on as if nobody had been asked.
# * The watchdog judges a round that produces nothing for 900 seconds to
#   be dead (``VERIFICATION_WATCHDOG_STALENESS_SECONDS``).  A round
#   blocked on a human produces nothing *by definition* — it is
#   waiting, not working — so an unwatched wait is a wait the watchdog
#   kills.
#
# The wait therefore does two things at once: it blocks the round until
# the question is settled, and it beats every 60 seconds so the block is
# visible to the watchdog.  The beat is written in the two places that
# mean "alive": the decision's own ``heartbeat_at`` (the row says the
# question is being waited for) and the round's registry handle (the
# ``hitl_wait`` stage the liveness probe reads).

#: The decision source a verification round waits on.
VERIFICATION_DECISION_SOURCE = "verification"

#: The decision source the *executor* asks its questions from.
#:
#: The architecture names the two sides a question can be asked from —
#: ``verification | execution`` — and this is the other one.  It lives
#: next to :data:`VERIFICATION_DECISION_SOURCE` on purpose: the two are
#: one vocabulary, not two, and the code that has to tell the sides
#: apart should not have to look them up in two places.
EXECUTOR_DECISION_SOURCE = "execution"

#: The registry stage a round carries while it waits for a human.
#:
#: The stage is what the liveness probe reads: a handle whose
#: ``last_progress_ts`` is fresh says "this plan is doing something",
#: and ``hitl_wait`` says *what* it is doing — waiting for a human,
#: rather than working.
HITL_WAIT_STAGE = "hitl_wait"

#: The registry identity of the handle a wait registers.
#:
#: A wait is not a verification point, so it does not borrow a VP's
#: identity: ``register_sub_agent`` reuses a handle with the same
#: ``(vp_id, attempt)`` and would reset a live sub-agent's stage and
#: drop the tool reference the watchdog needs in order to kill it.  A
#: wait gets an identity of its own instead.
HITL_WAIT_VP_ID = "hitl-wait"

#: How often the wait re-reads the question.
HITL_WAIT_POLL_INTERVAL = 2.0

#: How much waiting passes between two heartbeats.
#:
#: 60 seconds sits well inside the 900-second window the watchdog
#: judges a silent round by, so one missed beat is not a missed window.
HITL_WAIT_HEARTBEAT_INTERVAL = 60.0

#: The status a round rests in while it waits for a human.
#:
#: Not ``running``: a round that is waiting is not a round the watchdog
#: should judge.  Not a terminal status either: the *round* is over,
#: the *plan* is not — the plan is waiting for an answer.
HITL_WAIT_VERIFICATION_STATUS = "paused"

#: The states a question is still *open* in.
#:
#: ``pending`` is a question nobody has answered yet; ``paused`` is a
#: question nobody answered *and* that the backend may not answer in a
#: human's place.  Both are open: both may still be answered, and a
#: round that walks past either of them is a round that forgot it had
#: asked.
_HITL_OPEN_STATUSES = (DecisionStatus.PENDING.value, DecisionStatus.PAUSED.value)


def _open_verification_decisions(
    store: HitlStore, plan_id: str, round_number: Optional[int]
) -> List[Decision]:
    """The plan's open verification questions, oldest first.

    "Open" is the same pair of states the heartbeat is written over:
    ``pending`` and ``paused``.  A paused question is not a settled
    one — it is a question nobody answered yet, which a human may still
    answer — so the round must not walk past it either.

    Only questions the *verification* side asked are waited on: a
    question the execution side asked belongs to the executor, and the
    executor's own gate (``hitl.service.blocked_task_ids``) is what
    holds that work up.

    A question with no round attached is waited on whatever round is
    running.  It says something about the plan rather than about one
    round, and the round that is running is the one that has to hear
    the answer.
    """
    return [
        decision
        for decision in list_decisions(plan_id, None, store=store)
        if decision.source == VERIFICATION_DECISION_SOURCE
        and decision.status in _HITL_OPEN_STATUSES
        and (
            round_number is None
            or decision.round is None
            or decision.round == round_number
        )
    ]


def _wait_for_open_verification_decisions(
    plan_id: str,
    round_number: Optional[int] = None,
    *,
    poll_interval: float = HITL_WAIT_POLL_INTERVAL,
    heartbeat_interval: float = HITL_WAIT_HEARTBEAT_INTERVAL,
) -> Optional[Decision]:
    """Wait for the plan's open verification questions.

    The questions are waited for one at a time, oldest first: they are
    asked in order, and a human answers them in order.

    Returns the question that stopped the round, or ``None`` when the
    round may carry on.  Only a **paused** question stops it: that is a
    question nobody answered and that nobody may answer in a human's
    place, so the round waits for a human and the plan waits with it.
    A question that was answered, or that fell back to its default, is
    a question the round has its answer to; a question that was
    ``unroutable`` is one nobody was ever asked, and it blocks nothing.

    The wait itself ends on any of the settled states — including
    ``unroutable`` — and on the deadline.
    """
    with HitlStore(resolve_state_db_path()) as store:
        for decision in _open_verification_decisions(
            store, plan_id, round_number
        ):
            settled = _wait_for_one_verification_decision(
                plan_id,
                decision,
                store,
                poll_interval=poll_interval,
                heartbeat_interval=heartbeat_interval,
            )
            if settled.status == DecisionStatus.PAUSED.value:
                return settled
    return None


def _wait_for_one_verification_decision(
    plan_id: str,
    decision: Decision,
    store: HitlStore,
    *,
    poll_interval: float,
    heartbeat_interval: float,
) -> Decision:
    """Wait for one question, and beat while waiting.

    The handle is registered for the duration of the wait and
    unregistered when the wait ends: a handle that outlives its wait is
    a handle the watchdog will one day kill, and one the liveness probe
    will one day read as a live sub-agent.
    """
    handle = _server.sub_agent_registry.register_sub_agent(
        plan_id, HITL_WAIT_VP_ID, 1,
    )
    try:
        _mark_hitl_wait_progress(plan_id, handle)
        return wait_for_decision(
            decision.decision_id,
            store=store,
            on_heartbeat=lambda: _mark_hitl_wait_progress(plan_id, handle),
            poll_interval=poll_interval,
            heartbeat_interval=heartbeat_interval,
        )
    finally:
        _server.sub_agent_registry.unregister(plan_id, handle)


def _mark_hitl_wait_progress(plan_id: str, handle) -> None:
    """Say "the round is alive" where the watchdog looks for it.

    ``mark_progress`` refreshes the handle's ``last_progress_ts`` and
    carries the stage.  It is a no-op for a handle that is no longer
    registered, so a wait whose handle was cleaned up underneath it
    does not raise — it simply stops being visible.
    """
    try:
        _server.sub_agent_registry.mark_progress(
            plan_id, handle, stage=HITL_WAIT_STAGE
        )
    except Exception:  # noqa: BLE001 — a beat that fails is not a wait that fails
        _server.logger.exception(
            "[hitl_wait] the progress mark failed plan=%s", plan_id,
        )


def _pause_verification_for_human(
    plan_id: str, decision: Decision, round_number: Optional[int]
) -> None:
    """Rest the round on the question that is waiting for a human.

    The *round* stops; the *plan* does not.  Three things say so:

    * the **in-memory status** becomes ``paused``, so the watchdog stops
      watching a round that is not running.  A round that returned with
      its status still ``running`` is a round whose thread is dead, and
      the watchdog reads that as a dead round and fails the plan;
    * the **phase** returns to ``verification`` — the same resting
      phase the operator's stop uses — so the loop's fallback does not
      stamp the plan ``completed`` while it waits for an answer;
    * the **routing row** follows the phase, for the same reason the
      stop path moves it.

    The pause checkpoint itself is *not* written here: the watchdog
    tick owns that document, and it writes the moment it pauses the
    question — which is what names the question the round was waiting
    for.
    """
    _server.logger.warning(
        "[hitl_wait] round %s stopped — waiting for a human plan=%s "
        "decision=%s status=%s",
        round_number, plan_id, decision.decision_id, decision.status,
    )

    state = _server._verification_state.get(plan_id)
    if state:
        state["verification_status"] = HITL_WAIT_VERIFICATION_STATUS
        state["stop_reason"] = f"hitl_wait:{decision.status}"
        state["updated_at"] = datetime.now().isoformat()

    try:
        _server.PlanState(_server._plan_dir(plan_id)).transition_to(
            "verification"
        )
    except Exception:  # noqa: BLE001 — a phase that cannot move is not a reason to crash
        _server.logger.exception(
            "[hitl_wait] the phase could not be moved to 'verification' "
            "plan=%s", plan_id,
        )

    try:
        from state_machine.db.connection import open as _open_db_pause
        from state_machine.db.schema import migrate as _migrate_pause
        from state_machine.repositories.routing_repository import (
            RoutingRepository as _RoutingRepoPause,
        )

        conn = _open_db_pause(_server._state_db_path())
        try:
            _migrate_pause(conn)
            _RoutingRepoPause(conn).try_mark_phase(
                plan_id,
                (
                    "verification_running",
                    "verification_rerunning",
                    "verification_repairing",
                ),
                "verification",
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — the routing row is a mirror, not the truth
        _server.logger.exception(
            "[hitl_wait] the routing phase could not be moved to "
            "'verification' plan=%s", plan_id,
        )


def _lazy_check_verification(plan_id: str) -> None:
    """Probe a single running verification by inspecting its background thread
    AND the freshness of its log / results files.

    Detection ladder (any one match → ``stop_reason`` set → CAS to
    ``failed`` via ``_persist_verification_terminal``):

    - ``thread.is_alive()`` returns ``False`` →
      ``stop_reason = "verification_thread_died_unexpectedly"``
    - ``plans/{id}/logs/verification_*.log`` mtime older than
      ``VERIFICATION_WATCHDOG_STALENESS_SECONDS`` →
      ``stop_reason = "verification_log_stale"``
    - ``plans/{id}/verification_execution_results.json`` mtime older than
      ``VERIFICATION_WATCHDOG_STALENESS_SECONDS`` →
      ``stop_reason = "verification_results_stale"``

    2026-09-06:
    The previous implementation only flipped the in-memory state via
    ``_mark_verification_failed_dead`` and never advanced
    ``plan_routing.current_phase`` out of ``verification_*``. A plan whose
    orchestrator thread died silently (or got stuck waiting on a
    subprocess that the thread itself didn't own) would stay at
    ``verification_running`` forever, Feishu cards would render
    "still verifying" while the actual work was long dead. The fix is
    twofold: (1) widen the detection ladder to include log/results
    staleness — ``thread.is_alive()`` is unreliable for threads blocked
    on I/O; (2) call ``_persist_verification_terminal`` (which does the
    SQL CAS on ``plan_routing.current_phase`` and fires
    ``KIND_PLAN_CLOSED``) so the stuck plan transitions to ``failed``
    automatically.
    The original in-memory side effect is preserved for back-compat.

    Dedup: a per-plan "last action" timestamp in ``_WATCHDOG_STATS``
    prevents the lazy check (which runs every ``HEARTBEAT_INTERVAL``
    ticks) from firing the same ``_persist_verification_terminal``
    twice in quick succession. The CAS itself is already idempotent
    (``_persist_verification_terminal`` swallows ``ConflictError``),
    but the dedup also prevents the watchdog from emitting duplicate
    ``KIND_PLAN_CLOSED`` events on the state bus — duplicate events
    would each trigger a Feishu card rebuild and push.

    2026-09-07 observability: the lazy check now logs its entry,
    every detection-ladder probe result, and the final CAS action
    with the stop_reason. These are the canonical "what did the
    watchdog see" breadcrumbs for post-mortems on plans that look
    stuck at ``verification_running`` — operators should be able to
    grep ``[verification_watchdog]`` in server.log and reconstruct
    the detection decision per tick.

    2026-10-22: the tick now also owns the plan's human-in-the-loop
    deadlines, and it does so **before** the detection ladder, in this
    order:

    * the expiry sweep (``hitl.service.expire_due``) resolves the
      ``auto`` questions whose deadline has passed — a round waiting
      for a human must not keep waiting on a question that already has
      an answer;
    * a ``strict`` question whose deadline passed is *paused* rather
      than answered (see :func:`_handle_hitl_strict_timeout`).  There
      is no default answer the backend may use in a human's place, so
      the round stops here: the plan's phase does not advance, and the
      pause is recorded in ``pause_state.json`` so the round can be
      resumed once a human answers.  The question has to be the round's
      own for that: a question the *executor* asked is left ``pending``
      and keeps the executor's gate holding its task and the tasks
      downstream of it.

    Both are best-effort: a failure is logged and the tick carries on.
    """
    now = _hitl_now()
    _expire_hitl_decisions(now)
    paused_decision_id = _handle_hitl_strict_timeout(plan_id, now)

    state = _server._verification_state.get(plan_id)
    if not state or state.get("verification_status") != "running":
        return

    if paused_decision_id is not None:
        # The round is waiting for a human, not dead.  Nothing below
        # this line may fire: the ladder would read the round's
        # silence as a dead round and advance the plan's phase out of
        # verification — which is exactly what a strict timeout must
        # not do.
        _server.logger.warning(
            "[verification_watchdog] strict timeout — the round is paused "
            "plan=%s decision=%s",
            plan_id, paused_decision_id,
        )
        return

    _server.logger.debug(
        "[verification_watchdog] tick plan=%s round=%s status=%s",
        plan_id,
        state.get("verification_round"),
        state.get("verification_status"),
    )

    stop_reason: Optional[str] = None

    # --- Detection ladder ---
    # Thread-death is an immediate, unambiguous signal — if the
    # ``thread`` attribute refers to a Python Thread object that has
    # exited (or was never started), the verification cannot make any
    # more forward progress regardless of the grace period below.
    # Apply the grace-period guard only to the staleness checks
    # (which are intrinsically time-windowed signals).
    thread = state.get("thread")
    # Optimistic default: a missing/odd thread object (test doubles, a
    # recovered entry whose thread was never bound) must not be treated
    # as dead — see the AttributeError branch below.
    thread_alive = True
    if thread is not None:
        try:
            thread_alive = thread.is_alive()
        except AttributeError:
            # Test doubles / fake thread objects may not implement
            # ``is_alive()``. Treat them as alive so the lazy check
            # doesn't crash or rewrite the state.
            thread_alive = True
        if not thread_alive:
            stop_reason = "verification_thread_died_unexpectedly"
            _server.logger.warning(
                "[verification_watchdog] thread dead plan=%s", plan_id,
            )
            # 2026-09-14: the orchestrator thread is
            # gone — nothing will collect results or unregister its
            # sub-agent processes. Kill registered LLM subprocesses and
            # orphaned VP pytest/bash NOW instead of leaving zombies
            # (the registry tick would get to them eventually, but the
            # plan is being declared dead this instant).
            try:
                _dead_running = _server._running_vps_from_activity(
                    _server._read_latest_round_vp_activity(_server._plan_dir(plan_id))
                )
                _server._cleanup_dead_verification_processes(
                    plan_id, sorted(_dead_running)
                )
            except Exception:
                _server.logger.exception(
                    "[verification_watchdog] cleanup after thread death "
                    "failed plan=%s", plan_id,
                )

    if stop_reason is None:
        # Grace-period guard (kept from prior behavior): never judge a
        # verification as STALE within ``_VERIFICATION_STARTUP_GRACE_SECONDS``
        # of its ``started_at``. Without this guard, a race between
        # ``POST /api/verification/{id}/start`` and the next 30s heartbeat
        # tick could rewrite a freshly-initialized "running" state to
        # "failed" before the orchestrator has even had a chance to log
        # anything. Thread-death above does NOT honour this guard — see
        # the comment there for why.
        started_at_ts = state.get("started_at_ts")
        if started_at_ts is None:
            # Legacy state dict without the cached epoch float — derive it
            # lazily from the ISO timestamp. Missing / malformed → skip
            # this check entirely (better to under-protect than to crash
            # on a clock string we don't recognise).
            iso_started = state.get("started_at")
            if iso_started:
                try:
                    started_at_ts = datetime.fromisoformat(iso_started).timestamp()
                except ValueError:
                    started_at_ts = None
                if started_at_ts is not None:
                    state["started_at_ts"] = started_at_ts

        past_grace = (
            started_at_ts is not None
            and (time.time() - started_at_ts) >= _server._VERIFICATION_STARTUP_GRACE_SECONDS
        )

        if past_grace:
            # Log / results staleness — catches the case where the
            # orchestrator thread is still technically alive but is
            # blocked on a subprocess / lock / network call that's not
            # making forward progress.
            #
            # 2026-09-13: the log-staleness entry is a
            # *set* of globs, not one. A round writes progress to two
            # files and either one is enough to prove forward progress:
            #
            #   logs/verification_*.log  — group / VP lifecycle lines
            #   logs/vp_attempts/*.log   — per-attempt subagent log; the
            #                               ONLY file a long single-VP
            #                               retry touches (VP-034's
            #                               full-suite pytest runs ~15 min
            #                               per attempt, so the
            #                               orchestrator log legitimately
            #                               sits untouched for longer than
            #                               the 900s default threshold).
            #
            # Without the vp_attempts glob a healthy retry was
            # indistinguishable from a hung orchestrator, and the watchdog
            # killed the round with ``verification_log_stale``.
            #
            # 2026-10-14: third glob, ``execution.log``, for the same
            # reason and the same class of bug — a phase that is busy
            # but does not write the file this watchdog reads.
            #
            # Production, a 2026-10-12 plan,
            # round 1: the round log went silent for 52 min and the
            # watchdog killed a healthy round with ``verification_log_stale``
            # at the 45-min hard ceiling, 7 min before the round actually
            # finished. What it was doing during those 52 min is on
            # record in ``execution.log`` and nowhere else — every 300s
            # ``provider_quota_window_wait`` probe, a line the
            # ``_quota_wake_probe_sec`` cadence emits while a dispatch is
            # parked on a closed quota window:
            #
            #   10:30:19  last round-log line (binary_rebuild_attempted)
            #   10:42:03  provider_quota_window_wait_entered  <- execution.log
            #   10:47 / 10:52 / ... / 11:17   one probe line each
            #   11:15:18  watchdog: verification_log_stale, age 2700s = hard cap
            #   11:22:07  provider_quota_window_wait_ended    <- execution.log
            #   11:22:xx  round log resumes (judgment_heartbeat x6)
            #
            # So the round was alive for the whole window and the watchdog
            # was structurally unable to see it: the wait writes
            # ``execution.log`` (``ExecutionLogger._log_file``,
            # ``execution_logger.py``), and this set read neither that nor
            # anything the wait touched. Busy, read as dead — the identical
            # failure the vp_attempts glob fixed one release earlier, one
            # file over.
            #
            # Reading ``execution.log`` rather than special-casing "is this
            # round parked on quota?" is deliberate. The wait ALREADY emits
            # a periodic line; the watchdog simply was not looking at the
            # file it lands in, so there is nothing to invent and no new
            # plumbing to thread in. And it stays honest under a deadlock in
            # a way a "currently waiting" predicate cannot: a predicate
            # would have to be answered by the very thing being supervised
            # (a process-global in ``quota_watchdog``, which has no notion
            # of a plan at all — see ``waiting_dispatches``), so a waiter
            # wedged mid-wait would still report itself as waiting. Here the
            # evidence is a write that stops when the work stops: nothing
            # writes ``execution.log`` while a round is wedged, so a real
            # stall still goes quiet and is still caught — at the 1h hard
            # ceiling instead of the 15-min threshold.
            plan_dir = _server._plan_dir(plan_id)
            stale_latest: Optional[float] = None
            for glob_pattern, reason in (
                (
                    (
                        "logs/verification_*.log",
                        "logs/vp_attempts/*.log",
                        "execution.log",
                    ),
                    "verification_log_stale",
                ),
                ("verification_execution_results.json", "verification_results_stale"),
            ):
                latest = _server._latest_mtime(plan_dir, glob_pattern)
                if latest is not None and (
                    time.time() - latest
                ) > _server.VERIFICATION_WATCHDOG_STALENESS_SECONDS:
                    age = time.time() - latest
                    _server.logger.warning(
                        "[verification_watchdog] staleness plan=%s "
                        "file=%s age=%.1fs threshold=%s reason=%s",
                        plan_id, glob_pattern, age,
                        _server.VERIFICATION_WATCHDOG_STALENESS_SECONDS, reason,
                    )
                    stop_reason = reason
                    stale_latest = latest
                    break

            # --- 2026-09-14 triage ---
            # A stale round log is NOT proof of death while a VP is
            # legitimately mid-flight: a full-suite automated_test VP
            # streams progress to /tmp without touching the round log
            # for 18+ minutes (VP-023 was falsely flagged while its
            # pytest was healthy). Probe liveness before concluding:
            #   * fresh signal            → suppress, keep watching;
            #   * no signal, < hard cap   → hold off (agent may not
            #     follow the tee naming convention);
            #   * no signal, >= hard cap  → truly dead: kill leftovers
            #     (registered handles + /tmp-matched orphans), flag.
            # With NO running VP in the newest round log there are two
            # very different situations, distinguished by the thread:
            #   * thread DEAD        → T1 above already fired this tick;
            #   * thread ALIVE       → the round is in a POST-EXECUTION
            #     phase (report judgment)
            #     which runs entirely in-process and writes nothing to
            #     the plan dir. Give it the hard cap before stamping —
            #     this exact case terminalises a round while its
            #     judgment sub-agents are still working, because the
            #     staleness signal has no way to see in-process work.
            #     A deadlock is still caught — at the hard cap instead
            #     of the normal threshold. (``_judgment_heartbeat`` in
            #     ``verification_agent`` is the primary fix: the
            #     judgment phase now keeps the round log fresh, so this
            #     branch is the safety net for rounds that predate it.)
            if stop_reason in (
                "verification_log_stale", "verification_results_stale",
            ):
                _triage_running = _server._running_vps_from_activity(
                    _server._read_latest_round_vp_activity(plan_dir)
                )
                _stale_age = (
                    time.time() - stale_latest
                    if stale_latest is not None else 0.0
                )
                if not _triage_running and thread_alive and (
                    _stale_age < _server.VERIFICATION_HARD_STALE_SECONDS
                ):
                    _server.logger.info(
                        "[verification_watchdog] stale hold — no VP in "
                        "flight but thread alive (post-execution phase) "
                        "plan=%s age=%.1fs cap=%.0fs",
                        plan_id, _stale_age,
                        _server.VERIFICATION_HARD_STALE_SECONDS,
                    )
                    return
                if _triage_running:
                    _alive, _detail = _verification_liveness_probe(
                        plan_id, sorted(_triage_running)
                    )
                    if _alive:
                        _server.logger.info(
                            "[verification_watchdog] stale suppressed — "
                            "vp alive plan=%s vps=%s detail=%s",
                            plan_id, sorted(_triage_running), _detail,
                        )
                        return
                    if _stale_age < _server.VERIFICATION_HARD_STALE_SECONDS:
                        _server.logger.info(
                            "[verification_watchdog] stale hold — hard cap "
                            "not reached plan=%s age=%.1fs cap=%.0fs vps=%s",
                            plan_id, _stale_age,
                            _server.VERIFICATION_HARD_STALE_SECONDS,
                            sorted(_triage_running),
                        )
                        return
                    _server.logger.warning(
                        "[verification_watchdog] truly dead past hard cap "
                        "— killing leftovers plan=%s age=%.1fs vps=%s",
                        plan_id, _stale_age, sorted(_triage_running),
                    )
                    _server._cleanup_dead_verification_processes(
                        plan_id, sorted(_triage_running)
                    )

    if stop_reason is None:
        return

    # --- Dedup ---
    # If we already acted on this plan within the last 60s, skip. The
    # CAS in ``_persist_verification_terminal`` is itself idempotent
    # (it swallows ConflictError on a re-CAS attempt), but emitting
    # duplicate ``KIND_PLAN_CLOSED`` events would cause duplicate
    # Feishu card pushes — see 2026-09-06 plan rationale.
    per_plan = _server._WATCHDOG_STATS["per_plan_last_action_ts"]
    last_action_ts = per_plan.get(plan_id, 0.0)
    if time.time() - last_action_ts < 60.0:
        _server.logger.debug(
            "[verification_watchdog] dedup-hold plan=%s "
            "last_action_age=%.1fs stop_reason=%s",
            plan_id, time.time() - last_action_ts, stop_reason,
        )
        return

    # --- Existing in-memory side effect (kept for back-compat) ---
    # ``_mark_verification_failed_dead`` updates ``_verification_state``
    # in-memory + writes the side-channel ``.verification_runtime.json``
    # file. It's harmless to keep calling here — the plan_routing CAS
    # below is the new behavior this whole function adds.
    _server._mark_verification_failed_dead(plan_id)

    # --- NEW: advance plan_routing.current_phase out of verification_* ---
    # This is the missing piece. ``_persist_verification_terminal`` does
    # SQL CAS on plan_routing.current_phase (verification_running /
    # rerunning / repairing → failed), updates plan_verification, and via
    # ``VerificationRepository.complete_round`` fires KIND_PLAN_CLOSED
    # on the state bus. The Feishu notifier is already subscribed to
    # that event so the card flips from "still verifying" to "failed"
    # within seconds — no manual intervention needed.
    _server.logger.warning(
        "[verification_watchdog] firing plan=%s stop_reason=%s",
        plan_id, stop_reason,
    )
    try:
        # ``chain_ending=True`` — the watchdog only fires once the
        # verification thread is dead or has stopped writing progress,
        # so there is no live loop left to protect. Passing it
        # explicitly (instead of relying on ``stop_reason`` membership)
        # means a future watchdog stop reason cannot silently leave the
        # plan stuck at ``current_phase='verification_running'``.
        _server._persist_verification_terminal(
            plan_id, "failed", stop_reason, chain_ending=True
        )
        _server._record_watchdog_action(plan_id, stop_reason)
        # Also rewrite the ``plan_routing.verification`` JSON column so
        # ``/api/plan/{id}/summary`` reflects the new status (it reads
        # the verification sub-state via
        # ``PlanState._sqlite_row_to_state``). Without this, the Feishu
        # card would keep rendering "still verifying" even though the
        # SQL CAS just transitioned the plan to ``failed``.
        _server._update_plan_state_to_terminal(plan_id, stop_reason)
        _server.logger.warning(
            "[verification_watchdog] completed plan=%s", plan_id,
        )
    except Exception:
        _server.logger.exception(
            "[verification_watchdog] _persist_verification_terminal failed for plan=%s",
            plan_id,
        )


def _lazy_check_sub_agents() -> None:
    """Find sub-agents whose ``last_progress_ts`` is stale and force-fail them.

    Called from ``HeartbeatMonitor._check_once`` (30s tick). Mirrors the
    per-thread checks in ``_lazy_check_execution`` / ``_lazy_check_verification``
    but operates on the ``sub_agent_registry`` — keyed by plan_id,
    containing ``SubAgentHandle``s with a ``scoped_tool`` reference the
    watchdog can reach into to kill the underlying ``Popen``.

    Only sub-agents that have a subprocess are in scope. A verification
    point asleep on a closed quota window is filtered out inside
    :meth:`SubAgentRegistry.find_stale`, before this function sees it:
    such a point has no process to kill, no output to read, and no age
    that means anything, and letting it through would both spend the
    kill path on nothing and discard the handle that protects the
    process which does start once the window reopens. A wait that long
    is the provider's schedule, not a stall — the quota watchdog keeps
    probing it without a ceiling (see ``backend/quota_watchdog.py``),
    and this sweep is for the ones it cannot help.

    Threshold is loaded from ``SUB_AGENT_STALE_THRESHOLD_SEC`` env var
    at registry import time (default 1200s). Operators should not tune
    this to *less* than ``HARD_WALL_CLOCK_CAP_SECONDS`` (3600) —
    doing so would race normal slow LLM responses.
    """
    try:
        stale = _server.sub_agent_registry.find_stale(_server.SUB_AGENT_STALE_THRESHOLD_SEC)
    except Exception:
        # Registry read must never crash the heartbeat loop.
        return
    for handle in stale:
        try:
            # Second-chance liveness check — a long-running sub-agent
            # (e.g. pytest verification re-running the suite inside one
            # attempt) only calls ``mark_progress`` at stage transitions,
            # so ``last_progress_ts`` can age past the threshold even
            # while the sub-agent is actively streaming stdout. Read
            # ``scoped_tool._last_output_ts`` (updated on every stdout
            # line) and skip the kill if it's still fresh. Without this,
            # the watchdog killed healthy 12-minute pytest runs at the
            # 20-minute mark.
            #
            # A ``None`` there means the subprocess has produced nothing
            # at all this call. That is either a call that has only just
            # started or a dispatch parked on a quota window — and the
            # parked case is already gone, filtered upstream, because it
            # is the one with no process behind it.
            scoped_tool = getattr(handle, "scoped_tool", None)
            if scoped_tool is not None:
                last_output = getattr(scoped_tool, "_last_output_ts", None)
                if last_output is not None:
                    idle_for = time.monotonic() - last_output
                    if idle_for < _server.SUB_AGENT_STALE_THRESHOLD_SEC:
                        # Sub-agent is still streaming stdout — refresh
                        # last_progress_ts so the next tick doesn't
                        # immediately re-flag this handle.
                        try:
                            _server.sub_agent_registry.mark_progress(
                                handle.plan_id, handle, stage=handle.stage
                            )
                        except Exception:
                            pass
                        continue
            _server._handle_stuck_sub_agent(handle.plan_id, handle)
        except Exception:
            # Single stuck handle must not stop the watchdog.
            _server.logger.exception(
                "sub_agent_watchdog_failed plan_id=%s vp_id=%s",
                handle.plan_id, handle.vp_id,
            )


def _kill_sub_agent_process(handle) -> None:
    """Best-effort SIGTERM → SIGKILL kill of the sub-agent's Popen.

    Mirrors the escalation in ``ClaudeCodingTool._graceful_shutdown``
    (``backend/coding_tool.py:1237``) but reaches directly via the
    ``_current_process`` private attribute — the sub-agent registered
    a *fresh* ``ClaudeCodingTool`` instance (``scoped_tool``) so its
    cleanup path is independent of the parent's lifecycle.

    Never raises — a kill failure is logged but not propagated so the
    watchdog loop can continue to the next handle.
    """
    scoped_tool = getattr(handle, "scoped_tool", None)
    if scoped_tool is None:
        return

    # Acquire the tool's own process lock so we don't race with
    # _graceful_shutdown inside the tool (which sets _current_process
    # to None under the same lock).
    process_lock = getattr(scoped_tool, "_process_lock", None)
    proc = None
    if process_lock is not None:
        try:
            with process_lock:
                proc = scoped_tool._current_process
                scoped_tool._current_process = None
        except Exception:
            proc = getattr(scoped_tool, "_current_process", None)
    else:
        proc = getattr(scoped_tool, "_current_process", None)

    if proc is None:
        return

    try:
        if proc.poll() is not None:
            # Already exited — nothing to kill.
            return
    except Exception:
        # poll() failing means the OS-level handle is broken; skip.
        return

    # Phase 1: SIGTERM, give the Claude wrapper 5s to clean up stdin
    # and exit cleanly. Avoids corrupting JSONL streams the wrapper
    # may be flushing.
    try:
        _server.kill_process_group(proc, sig=signal.SIGTERM, wait_timeout=5.0)
    except Exception:
        pass

    try:
        if proc.poll() is not None:
            return
    except Exception:
        return

    # Phase 2: SIGKILL escalation. Same helper, default sig=SIGKILL.
    try:
        _server.kill_process_group(proc, sig=signal.SIGKILL, wait_timeout=5.0)
    except Exception:
        pass


def _handle_stuck_sub_agent(plan_id: str, handle) -> None:
    """Force a hung sub-agent into the ``failed`` state.

    Steps:
      1. SIGTERM-→SIGKILL the sub-agent's ``Popen``.
      2. Unregister from the registry so we don't refire on next tick.
      3. Write an auditable JSON-line entry to
         ``plans/{id}/logs/vp_attempts/`` so an operator can grep for
         stalled VPs after the fact.
      4. Mark the surrounding verification round as failed with
         ``stop_reason="sub_agent_did_not_progress"``, persisting to
         ``plan_state.json`` so the next API call returns truth.
    """
    # 1) kill
    try:
        _server._kill_sub_agent_process(handle)
    except Exception:
        _server.logger.exception(
            "sub_agent_kill_failed plan_id=%s vp_id=%s",
            plan_id, handle.vp_id,
        )

    # 2) unregister
    try:
        _server.sub_agent_registry.unregister(plan_id, handle)
    except Exception:
        pass

    # 3) audit log entry
    try:
        plan_dir = _server._plan_dir(plan_id)
        attempt_dir = plan_dir / "logs" / "vp_attempts"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        ts_ms = int(time.time() * 1000)
        attempt_log = attempt_dir / f"vp_attempt_{handle.vp_id}_hung_{ts_ms}.log"
        age = handle.age_from_last_progress()
        with open(attempt_log, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "event": "sub_agent_did_not_progress",
                "data": {
                    "vp_id": handle.vp_id,
                    "attempt": handle.attempt,
                    "stage": handle.stage,
                    "age_from_last_progress": age,
                    "started_at": handle.started_at,
                    "last_progress_ts": handle.last_progress_ts,
                    "threshold_seconds": _server.SUB_AGENT_STALE_THRESHOLD_SEC,
                },
            }, default=str) + "\n")
    except Exception:
        _server.logger.exception(
            "sub_agent_audit_log_failed plan_id=%s vp_id=%s",
            plan_id, handle.vp_id,
        )

    # 4) signal retry to executor — DO NOT abort the round.
    #
    # Old behavior (pre-2026-09-07 plan): flip verification_status=
    # "failed" + transition PlanState + persist .verification_runtime
    # .json. That aborted the whole round, leaving every other VP
    # un-run (one stuck VP took down the entire plan).
    #
    # New behavior: the verification thread is awaiting ``query_json``
    # inside ``_execute_attempt``. The SIGKILL we sent in step 1 will
    # cause ``query_json`` to raise an I/O exception that the existing
    # ``except Exception`` catches. The catch branch reads
    # ``watchdog_kill_count``: 1 → raise ``WatchdogKilledError`` →
    # ``_self_heal_loop`` catches it and retries once with a fresh
    # ``scoped_tool``. If the retry also hangs (count reaches 2)
    # ``_execute_attempt`` returns ``Verdict(verdict="SKIPPED", ...)``
    # which ``_record_result`` routes into ``_skipped_vps``
    # (verification_executor.py:1254-1256). Round continues to the
    # next VP either way.
    with _server.sub_agent_registry._lock:
        handle.watchdog_kill_count += 1
        current_kill_count = handle.watchdog_kill_count

    _server._WATCHDOG_STATS["sub_agent_killed_total"] += 1
    if current_kill_count >= 2:
        _server._WATCHDOG_STATS["sub_agent_skipped_total"] += 1

    _server._record_watchdog_action(plan_id, "sub_agent_kill_signal_sent")
    _server.logger.warning(
        "[sub_agent_watchdog] kill signalled plan=%s vp=%s "
        "attempt=%s kill_count=%s (round continues; executor will "
        "retry or SKIP)",
        plan_id, handle.vp_id, handle.attempt, current_kill_count,
    )


def _repair_exit_writer(
    plan_id: str,
    plan_dir: Path,
) -> Callable[[Any, Optional[int], str], None]:
    """Return the one-shot writer for a repair round's exit event.

    Both repair paths — the synchronous :func:`_run_repair_execution`
    and the asynchronous :func:`_run_repair_execution_async` — have to
    close the pid they opened. A pid the backend has stopped tracking
    must not stay live in the ledger: the reaper goes on believing a
    process nobody watches is still running, and goes on collecting it,
    round after round, for a process that ended long ago.

    One writer, one rule, so the two paths cannot drift apart:

    * the first call wins, and every later call is a no-op — the exit
      of a round is written once, not once per branch;
    * the ledger is evidence, not a gate. A read-only plan directory
      or a full disk must not cost the operator the repair round they
      asked for. The failure is logged and the round continues.
    """
    written = False

    def _write(
        process: Any,
        exit_code: Optional[int],
        detail: str,
    ) -> None:
        nonlocal written
        if written:
            return
        pid = getattr(process, "pid", None) if process is not None else None
        if not pid:
            # No pid means nothing was spawned — there is no process to
            # close, and an exit for it would be a phantom.
            return
        written = True
        try:
            # Imported lazily, not at module level: ``server`` imports
            # this module, so a module-level import of
            # ``routes.execution`` (which ``server`` imports too) would
            # add a second edge to a cycle that already exists — and
            # importing this module first already trips it. The lazy
            # import resolves the name at call time, when both modules
            # are loaded.
            from routes.execution import _ledger_write_exit
            _ledger_write_exit(plan_dir, pid, exit_code, detail=detail)
        except Exception:  # noqa: BLE001 — the ledger must never break a round
            _server.logger.warning(
                "repair_executor_exit_ledger_write_failed plan=%s pid=%s",
                plan_id,
                pid,
                exc_info=True,
            )

    return _write


def _run_repair_execution(plan_id: str, project_dir: Path, tool: Optional[str] = None) -> bool:
    """Run repair task execution via CLI.

    Returns True if execution succeeded, False otherwise.

    2026-09-06:

    Previously this function inlined its own ``subprocess.Popen`` and
    called ``process.wait()`` without draining stdout — every repair
    round that produced >64KB of output (macOS pipe buffer limit)
    blocked the subprocess on its next ``write()`` and the function
    hung forever. The fix is to route the spawn through the shared
    :func:`_spawn_executor_subprocess` helper, which redirects
    stdout to a log file (no pipe capacity limit) instead of
    ``subprocess.PIPE``. The repair path is synchronous — we
    ``wait()`` for the subprocess to finish — but with stdout
    pointed at a file there is no pipe-buffer deadlock risk, so a
    simple blocking wait is safe.

    2026-09-08 (single-writer refactor): the previous
    ``_merge_repair_tasks_into_plan`` step that wrote a sibling
    ``tasks_with_repair_round_<N>.json`` file is REMOVED. The
    orchestrator's :meth:`check_cycle_conditions` now writes each
    repair task to ``state.db`` via
    :meth:`PlanTaskRepository.add_task`; the executor subprocess's
    :meth:`AutonomousAgent._load_tasks` Phase-2 reconcile step
    picks them up as state.db orphans and injects them into the
    DAG. ``tasks.json`` stays the single canonical disk artifact.
    The ``verification_tasks_file`` parameter is gone — the
    ``verification_tasks_round_N.json`` snapshot is no longer read
    by the executor subprocess.

    2026-10-30 (process ledger):

    The repair round is recorded on the process ledger under its own
    role, ``repair_executor``. The reaper reaps by role and in a fixed
    order (children first, parents last), so a repair executor
    recorded as a plain ``executor`` is indistinguishable from the
    plan's main executor — and the ordering that lets the repair round
    be torn down *before* the main executor is lost.

    The exit is written after ``process.wait()``: a process that has
    not ended cannot have exited. The ``finally`` writes it even when
    the round blows up before or during the wait — a pid the backend
    has stopped tracking must not stay live in the ledger, or the
    reaper goes on believing a process nobody watches is still
    running. A ledger that cannot be written never breaks the round:
    the ledger is evidence, not a gate.
    """
    backend_dir = _server._BACKEND_DIR
    plan_dir = _server._plan_dir(plan_id)
    tasks_file = plan_dir / "tasks.json"

    log_path = plan_dir / "logs" / f"repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    process: Optional[subprocess.Popen] = None
    # One writer for both repair paths — see :func:`_repair_exit_writer`.
    write_exit = _repair_exit_writer(plan_id, plan_dir)

    try:
        # Single shared helper — same drain strategy as
        # start_execution's _run() thread. stdout → log file so
        # pipe-buffer deadlock is impossible regardless of output
        # volume (R1-5's full pytest run was the trigger case).
        #
        # The canonical ``tasks.json`` is passed (no
        # ``--verification-tasks``): repair tasks live in
        # ``state.db`` and the executor reconciles them via
        # ``_load_tasks`` Phase 2.
        #
        # ``role="repair_executor"``: this executor is the plan's
        # child, and the reaper has to be able to say so.
        process, _log = _server._spawn_executor_subprocess(
            plan_id=plan_id,
            backend_dir=backend_dir,
            project_dir=project_dir,
            tasks_file=tasks_file,
            tool=tool,
            extra_args=None,
            log_path=log_path,
            role="repair_executor",
        )
        # Synchronous wait is safe here: the subprocess writes to a
        # log file, not a PIPE, so it never blocks waiting for a
        # reader. This mirrors how a supervisor handles long-running
        # agent subprocesses — see ``cmd.WaitDelay = 3 * time.Second`` for the Go reference.
        returncode = process.wait()
        # The process really has ended — the exit is written here and
        # nowhere earlier.
        write_exit(process, returncode, "repair_finished")
        return returncode == 0
    finally:
        # The exit for a round that died before or during the wait:
        # the pid is not live any more, so the ledger must not say it
        # is. No-op after the normal path already wrote it.
        write_exit(process, None, "repair_aborted")
        # Belt-and-suspenders: if any legacy ``tasks_with_repair_round_*.json``
        # file is still sitting in the plan directory from a pre-2026-09-08
        # run, remove it so it doesn't get picked up by a future read.
        # (The new code path never writes such a file.)
        try:
            for stale in plan_dir.glob("tasks_with_repair_round_*.json"):
                try:
                    stale.unlink()
                    print(f"[Repair] Cleaned up legacy merged tasks file {stale.name}")
                except OSError:
                    pass
        except OSError:
            pass


def _mark_verification_round_running(plan_id: str, round_num: int) -> None:
    """Flip the plan's in-memory verification entry to ``running`` for round N.

    2026-09-15 — both round starters (the auto-loop's ``for round_num``
    body and the post-repair re-entry) left the PREVIOUS round's terminal
    status in ``_verification_state`` until the next terminal write. Since
    ``/api/system/active`` only reports a verification whose status is
    ``running``/``repairing``/``rerunning``, the operator saw
    ``total_active: 0`` — and the card showed the old verdict — for the
    entire duration of round N ≥ 2 while VPs were actively running
    (observed live through round 2). Re-assert the round identity at
    round start: status, round number, cleared stop reason, fresh
    timestamp — and the liveness handle, which the post-repair re-entry
    otherwise left pointing at the previous round's (dead) thread.
    """
    with _server._verification_lock:
        state = _server._verification_state.setdefault(plan_id, {})
        state["verification_status"] = "running"
        state["verification_round"] = round_num
        state["stop_reason"] = None
        state["updated_at"] = datetime.now().isoformat()
        # 2026-09-15 — rebind the liveness handle to the thread that is
        # about to drive this round. The post-repair re-entry runs
        # ``start_verification_cycle`` synchronously on the repair-watcher
        # thread and never passes through the binding in
        # ``POST /api/verification/{id}/start``, so ``thread`` kept
        # pointing at the round-1 orchestrator thread — dead since round 1
        # ended. ``HeartbeatMonitor`` polls ``thread.is_alive()`` on a 30s
        # tick and treats a dead handle as an immediate, unambiguous
        # signal, so it fired ``verification_thread_died_unexpectedly``
        # seconds after a round started, terminally failing the plan
        # and killing its registered sub-agents while a VP was still
        # running (the round-start log line and the thread-death
        # declaration land within the same few seconds).
        #
        # Both call sites are the thread that drives the round — the
        # auto-loop iterates its rounds on one thread — so rebinding is
        # correct for each and a no-op where the binding already happened.
        state["thread"] = threading.current_thread()


def _plan_next_round(
    current_row: Optional[Dict[str, Any]],
) -> Tuple[int, int, bool]:
    """Decide the next verification round against the plan's IMMUTABLE cap.

    Returns ``(next_round, max_rounds, exhausted)``.

    2026-09-15: ``max_rounds`` is the budget the plan was
    set up with and nothing may raise it. The post-repair re-entry used to
    do ``min(max_rounds + 1, 10)`` on every pass — "so the chain has one
    more iteration of headroom" — which meant the cap never actually
    bound: ``next_round`` was computed as ``round + 1`` while the cap grew
    in lockstep, so a plan whose rounds kept failing could never reach
    ``max_rounds_reached`` — and the two stores end up disagreeing about
    how many rounds ran, because ``plan_state.json`` and ``state.db``
    record the number at different moments. The cap must not be raised on
    every iteration.

    Extracted from the ``_on_repair_complete`` closure so the decision is
    reachable from tests — the closure itself needs a whole running
    verification loop to exercise.

    Scope -- read this before reusing the function:

    _plan_next_round answers "which round comes after a round that REALLY
    finished": it always returns current.round + 1. It is NOT the resume
    decision. POST /api/verification/{id}/start asks _resolve_start_round
    instead, which returns the interrupted round itself when the plan
    carries a pause checkpoint or a running / loop_stopped row. The two are
    deliberately separate: a finished round advances, an interrupted round
    does not.
    """
    if not current_row:
        # No row yet: the plan starts at round 1 against the default cap.
        return 1, _server.DEFAULT_MAX_VERIFICATION_ROUNDS, False
    max_rounds = int(
        current_row.get("max_rounds") or _server.DEFAULT_MAX_VERIFICATION_ROUNDS
    )
    next_round = int(current_row.get("round") or 0) + 1
    return next_round, max_rounds, next_round > max_rounds


def _bind_verification_state_conn(plan_id: str, conn: Any) -> None:
    """Install ``conn`` as the plan's state.db handle, closing any previous.

    2026-09-15 — the post-repair re-entry used to do::

        _vc, _, repo = _open_verification_state()
        _vc.close()          # ← kills the repo handed to the new round

    and every write through that repository then raised
    ``ProgrammingError: Cannot operate on a closed database`` (observed
    live across all of round 2 on a production plan): per-VP progress froze
    and the plan vanished from /api/system/active while it was running.
    Binding instead of closing keeps the round writable; at most one
    handle per plan is live at a time.
    """
    with _server._verification_lock:
        slot = _server._verification_state.setdefault(plan_id, {})
        prev = slot.get("_state_db_conn")
        if prev is not None and prev is not conn:
            try:
                prev.close()
            except Exception:
                pass
        slot["_state_db_conn"] = conn


def _release_verification_state_conn(plan_id: str) -> None:
    """Pop and close the plan's slot-bound state.db connection.

    2026-09-15 — the post-repair re-entry (``_on_repair_complete``) binds
    its own connection into ``_verification_state[plan_id]`` so the
    repository it hands to the new round stays writable (see the fix
    comment there). On the repair-subprocess path the callback runs the
    whole round on the watcher thread, which is not the thread that
    normally owns the slot — so release the handle explicitly once the
    callback returns. Idempotent: popping an absent/already-closed
    handle is a no-op.
    """
    state = _server._verification_state.get(plan_id)
    if not state:
        return
    conn = state.pop("_state_db_conn", None)
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _write_project_dir_rejection(plan_dir: Path, exc: "ProjectDirInvalidError") -> None:
    """Append the refused ``project_dir`` to the plan's execution log.

    The dispatch is rejected before anything observable happens, which
    makes the log the only place the operator can learn *which*
    directory was refused — the plan id alone does not say, and the
    refusal itself never reaches the executor's own (never-written)
    log file.

    The line carries the path verbatim, because the path is the whole
    content of the error: a resolved path would hide the difference
    between "the operator typed a typo" and "the worktree was deleted
    under us".

    Failures to write are swallowed: a log line is a diagnostic, and
    losing it must not turn a clean rejection into a crash.
    """
    try:
        logs_dir = plan_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        with (logs_dir / "execution_start.log").open("a", encoding="utf-8") as handle:
            handle.write(
                f"{datetime.now().isoformat()} project_dir rejected: "
                f"{exc.project_dir} ({exc.reason})\n"
            )
    except Exception:
        _server.logger.exception(
            "[repair_async] could not write project_dir rejection log plan=%s",
            plan_dir.name,
        )


def _run_repair_execution_async(
    plan_id: str,
    project_dir: Path,
    tool: Optional[str] = None,
    on_complete: Optional[Callable[[int], None]] = None,
    outcome: "Optional[_VerificationLoopOutcome]" = None,
    scene: Optional[str] = None,
) -> dict:
    """Spawn executor subprocess for repair tasks; return immediately.

    Returns ``{"status": "started", "pid": <pid>}`` synchronously. The
    subprocess runs in background; ``on_complete`` fires once on
    subprocess exit (success or failure). On spawn failure,
    ``on_complete(-1)`` fires before this function returns.

    2026-10-31: ``scene`` names the provider-routing scene the executor
    subprocess runs under.  The executor is a CHILD PROCESS, so it shares
    no Python state with this dispatcher: the scene has to cross the
    process boundary as an environment variable
    (``PDT_EXECUTOR_SCENE``), which is what
    ``agent._executor_scene_from_env()`` reads back when it builds the
    coding tool.  A scene that stops here is a scene the executor never
    hears about.  ``None`` (the default) adds no variable at all, so an
    ordinary product-code repair keeps the provider chain it always had.

    2026-10-23: a ``project_dir`` that is not an existing directory is
    refused BEFORE the phase is stamped and the subprocess is spawned,
    and the refusal returns
    ``{"status": "failed", "reason": "project_dir_invalid"}``. Nothing
    runs in that case, so the plan must not be left looking like it did
    — see the comment at the validation site below.

    2026-09-23: ``outcome`` is how the caller's
    :class:`_VerificationLoopOutcome` learns that the auto-verification
    loop is ending in a **hand-off** rather than a terminal verdict. The
    recording lives here, on both exits, rather than at the call sites:
    the dispatch is what creates the ambiguity, and
    ``test_repair_generation_failure_handling`` pins the call sites'
    shape (``return`` immediately after the call) for a reason worth
    keeping.
    """
    backend_dir = _server._BACKEND_DIR
    plan_dir = _server._plan_dir(plan_id)
    tasks_file = plan_dir / "tasks.json"
    log_path = plan_dir / "logs" / f"repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    # 2026-10-23: refuse an unusable working directory BEFORE anything
    # observable happens. The order matters: ``force_set_phase("executing")``
    # writes the phase the operator and the watchdog read, and the spawn
    # starts a subprocess whose own failure is invisible from here. So a
    # ``project_dir`` that is gone (a deleted worktree, a typo) used to
    # leave the plan stamped ``executing`` with nothing running — the
    # ghost state this check exists to prevent.
    #
    # The validation is the shared one (``project_dir_guard``), so the
    # repair path and the start-execution route cannot drift apart.
    try:
        project_dir = validate_project_dir(project_dir)
    except ProjectDirInvalidError as exc:
        _server.logger.error(
            "[repair_async] project_dir rejected plan=%s: %s", plan_id, exc)
        with _server._execution_state_lock:
            _state = _server._execution_state.setdefault(plan_id, {})
            _state.update({
                'status': 'failed',
                'stop_reason': 'project_dir_invalid',
                'project_dir': exc.project_dir,
                'ended_at': datetime.now().isoformat(),
            })
        _write_project_dir_rejection(plan_dir, exc)
        if on_complete is not None:
            try:
                on_complete(-1)
            except Exception:
                _server.logger.exception(
                    "[repair_async] on_complete(-1) crashed plan=%s", plan_id)
        return {'status': 'failed', 'reason': 'project_dir_invalid'}

    # Seed ``_execution_state`` BEFORE spawning so the watchdog sees the
    # new pid from the very first heartbeat tick. Without this, a 30s
    # window opens where ``_execution_state`` has no entry for plan_id
    # and the lazy check returns no-op.
    state: dict = {}
    with _server._execution_state_lock:
        state = _server._execution_state.setdefault(plan_id, {
            "logs": [],
            "project_dir": str(project_dir),
            "started_at": datetime.now().isoformat(),
            "ended_at": None,
            "stop_reason": None,
            "sync_targets": None,
        })
        state["status"] = "running"
        state["pid"] = None
        state["log_path"] = str(log_path)
        state["started_at"] = datetime.now().isoformat()
        state["ended_at"] = None
        state["stop_reason"] = None
        state["process"] = None
        # ``_source`` distinguishes repair subprocess from main execution
        # subprocess in watchdog / status / card rendering paths. Both
        # share ``_execution_state`` but the lifecycle and callback
        # semantics are different.
        state["_source"] = "repair_execution"
        state["_on_complete"] = on_complete

    # Mirror the main-execution path's plan_state transition. ``force_set_phase``
    # bypasses the legal-transition guard because the executor subprocess
    # has its own ``start_execution`` flow which will re-assert the phase.
    try:
        _server.PlanState(plan_dir).force_set_phase("executing")
    except Exception:
        _server.logger.exception("[repair_async] force_set_phase(executing) failed plan=%s", plan_id)

    # Spawn the subprocess. Same helper as main execution, so the
    # stdout→log-file redirect + no-PIPE-buffer deadlock fix from
    # plan 2026-09-06 carries over.
    #
    # ``role="repair_executor"``: this executor is the plan's child,
    # and the reaper has to be able to say so. It reaps by role and in
    # a fixed order (children first, parents last), so a repair
    # executor recorded as a plain ``executor`` is indistinguishable
    # from the plan's main executor — and the ordering that lets the
    # repair round be torn down *before* the main executor is lost.
    # This is the path production actually runs: the synchronous
    # :func:`_run_repair_execution` has no production caller.
    #
    # ``extra_env`` carries the scene (see the docstring): the executor is
    # a child process, so the environment is the only channel that reaches
    # it.  ``None`` means "no scene" — the helper adds nothing then, and
    # the ordinary repair dispatch is unchanged.
    extra_env = {"PDT_EXECUTOR_SCENE": scene} if scene else None
    try:
        process, _log = _server._spawn_executor_subprocess(
            plan_id=plan_id,
            backend_dir=backend_dir,
            project_dir=project_dir,
            tasks_file=tasks_file,
            tool=tool,
            extra_args=None,
            log_path=log_path,
            role="repair_executor",
            extra_env=extra_env,
        )
    except OSError as exc:
        # Spawn failed — mirror main execution's failure path.
        _server.logger.exception("[repair_async] spawn failed plan=%s: %s", plan_id, exc)
        state["status"] = "failed"
        state["stop_reason"] = "repair_spawn_failed"
        state["ended_at"] = datetime.now().isoformat()
        # Persist failure to plan_execution so /status endpoint sees it.
        try:
            from state_machine.db.connection import open as _open_db
            from state_machine.db.schema import migrate as _migrate
            from state_machine.repositories.execution_repository import (
                ExecutionRepository as _ExecRepo,
                PlanNotFoundError as _PlanNotFoundError,
            )
            _persist_conn = _open_db(_server._state_db_path())
            try:
                _migrate(_persist_conn)
                _ExecRepo(_persist_conn).update_status(plan_id, "failed")
            finally:
                _persist_conn.close()
        except _PlanNotFoundError:
            pass
        except Exception:
            _server.logger.exception(
                "[repair_async] update_status to plan_execution failed plan=%s",
                plan_id,
            )
        # Fire on_complete(-1) so the verification loop knows spawn
        # failed; the callback decides whether to retry / record_terminal.
        if on_complete is not None:
            try:
                on_complete(-1)
            except Exception:
                _server.logger.exception("[repair_async] on_complete(-1) crashed plan=%s", plan_id)
        # No subprocess of ours is running — but ``on_complete(-1)`` ran
        # inline above and may itself have re-entered the chain and
        # dispatched one, so ask the live record rather than assuming.
        if outcome is not None:
            outcome.handed_off = _server._plan_execution_in_flight(plan_id)
        return {"status": "failed", "pid": None}

    with _server._execution_state_lock:
        state["pid"] = process.pid
        state["process"] = process

    # Persist the new pid to state.db.plan_execution so the /status
    # endpoint (which reads from ExecutionRepository) reflects the
    # running subprocess.
    try:
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.execution_repository import (
            ExecutionRepository as _ExecRepo,
            PlanNotFoundError as _PlanNotFoundError,
        )
        _persist_conn = _open_db(_server._state_db_path())
        try:
            _migrate(_persist_conn)
            _ExecRepo(_persist_conn).update_fields(
                plan_id,
                project_dir=str(project_dir),
                exec_pid=process.pid,
                exec_status="running",
            )
        finally:
            _persist_conn.close()
    except _PlanNotFoundError:
        pass
    except Exception:
        _server.logger.exception(
            "[repair_async] update_fields to plan_execution failed plan=%s",
            plan_id,
        )

    # One writer for both repair paths — see :func:`_repair_exit_writer`.
    # The exit is written from the background thread, because the exit
    # happens there: this function returns while the subprocess is
    # still running.
    write_exit = _repair_exit_writer(plan_id, plan_dir)

    def _run() -> None:
        try:
            # ``process.wait()`` is safe — stdout/stderr redirected to
            # log file (no PIPE-buffer deadlock). The verification
            # watchdog is unaware of this thread, but the executor
            # watchdog (``_lazy_check_execution``) IS — it sees the
            # updated ``_execution_state[plan_id].pid`` and will fire
            # ``_mark_failed_dead`` if the subprocess crashes.
            process.wait()
            # The process really has ended — the exit is written here
            # and nowhere earlier. Without it the pid stays in the
            # live set of the ledger forever, and the reaper goes on
            # collecting a process that ended long ago.
            write_exit(process, process.returncode, "repair_finished")
            with _server._execution_state_lock:
                # 2026-10-23: the "finished cleanly" flag is the executor
                # watchdog's yield signal — ``_lazy_check_execution``
                # early-returns instead of firing ``_mark_failed_dead``
                # when it is set. It therefore has to mean "the
                # subprocess exited 0", nothing else. Writing it
                # unconditionally (as this path used to) told the
                # watchdog to stand down over a crashed executor, so a
                # non-zero exit was never accounted for by anything.
                #
                # The return code itself is recorded unconditionally.
                state["executor_returncode"] = process.returncode
                if process.returncode == 0:
                    # 2026-09-11 plan v11: set the watchdog-friendly
                    # "finished cleanly" flag BEFORE any subsequent state
                    # mutation, mirroring the main execution path's fix
                    # for the same race window.
                    state["executor_finished_cleanly"] = True
                    # ------------------------------------------------------------
                    # 2026-10-11: a 0 return code means "the subprocess did
                    # not crash", nothing more. The repair path used to read
                    # it as "the repair round is done", which is false
                    # whenever the executor bailed out early: ``agent.py``
                    # hits ``No schedulable micro-layer found`` and just
                    # ``break``s — no exception, no non-zero exit. That
                    # wrote ``exec_status='completed'`` with
                    # ``stop_reason IS NULL`` for a round whose repair
                    # tasks were still ``pending`` and would never run.
                    #
                    # So cross-check the authoritative runtime overlay the
                    # same way the main execution path does
                    # (``routes/execution.py`` → ``_count_unfinished_tasks``).
                    # That helper had exactly one caller — the main path —
                    # and the repair path was the blind spot.
                    # ------------------------------------------------------------
                    _unfinished = _count_unfinished_tasks(plan_id, tasks_file)
                    _left = int(_unfinished.get("pending_or_unstarted", 0) or 0)
                    if _left > 0:
                        # Accounting only. The unfinished rows stay exactly
                        # as they are (``pending`` / ``in_progress`` /
                        # ``failed``) — they genuinely did not run and are
                        # genuinely not entitled to run behind a failed
                        # upstream.
                        #
                        # In particular this does NOT mark them ``skipped``.
                        # The main path's ``partial_completion`` branch does,
                        # and copying that here would be a real weakening of
                        # the dependency contract: ``skipped`` is a terminal
                        # SUCCESS status that unlocks downstream under
                        # ``agent.is_dependency_ready``, so labelling a task
                        # "blocked behind a failed upstream" as skipped would
                        # let its downstream start without the upstream's
                        # output. Only the status and stop_reason are written.
                        state["status"] = "failed"
                        state["stop_reason"] = (
                            f"repair_exited_with_unfinished_tasks:{_left}"
                        )
                        state["repair_unfinished_tasks"] = _left
                        _server.logger.warning(
                            "[repair_async] executor exited 0 but %d "
                            "repair task(s) never reached a terminal "
                            "success state plan=%s round=%s — recording "
                            "failure instead of completion",
                            _left, plan_id, state.get("round"),
                        )
                    else:
                        state["status"] = "completed"
                        state["stop_reason"] = None
                else:
                    # Explicit False, not "leave it alone": the state
                    # dict is keyed by plan_id and survives across
                    # rounds, so a previous round's ``True`` would
                    # otherwise linger here and keep the watchdog
                    # yielding over this crash.
                    state["executor_finished_cleanly"] = False
                    state["status"] = "failed"
                    state["stop_reason"] = "repair_non_zero_exit"
                state["ended_at"] = datetime.now().isoformat()

            # Persist terminal status to plan_execution.
            try:
                from state_machine.db.connection import open as _open_db
                from state_machine.db.schema import migrate as _migrate
                from state_machine.repositories.execution_repository import (
                    ExecutionRepository as _ExecRepo,
                    PlanNotFoundError as _PlanNotFoundError,
                )
                _persist_conn = _open_db(_server._state_db_path())
                try:
                    _migrate(_persist_conn)
                    _ExecRepo(_persist_conn).update_status(
                        plan_id,
                        state["status"],
                    )
                finally:
                    _persist_conn.close()
            except _PlanNotFoundError:
                pass
            except Exception:
                _server.logger.exception(
                    "[repair_async] update_status to plan_execution failed plan=%s",
                    plan_id,
                )

            # Fire on_complete callback. The callback runs in this
            # background thread (NOT the FastAPI request thread).
            # It will route through ``_on_repair_complete`` which
            # bumps max_rounds, CASes routing stage, and invokes a
            # fresh ``start_verification_cycle`` to re-confirm VPs.
            cb = state.get("_on_complete")
            if cb is not None:
                try:
                    cb(process.returncode)
                except Exception:
                    _server.logger.exception(
                        "[repair_async] on_complete callback crashed plan=%s rc=%d",
                        plan_id,
                        process.returncode,
                    )
                finally:
                    # 2026-09-15: the callback runs the whole next
                    # verification round synchronously on THIS thread and
                    # binds its own state.db connection into
                    # ``_verification_state[plan_id]`` (see the
                    # re-entry block in ``_on_repair_complete``). Nothing
                    # else closes it on this path — the auto-loop thread
                    # that normally owns the slot has already returned —
                    # so release it here once the round is done.
                    _release_verification_state_conn(plan_id)
        except Exception:
            _server.logger.exception("[repair_async] background wait crashed plan=%s", plan_id)
            with _server._execution_state_lock:
                state["status"] = "failed"
                state["stop_reason"] = "process_died_unexpectedly"
                state["ended_at"] = datetime.now().isoformat()
            cb = state.get("_on_complete")
            if cb is not None:
                try:
                    cb(-1)
                except Exception:
                    _server.logger.exception(
                        "[repair_async] on_complete(-1) crashed plan=%s",
                        plan_id,
                    )
        finally:
            # The exit for a round that died before or during the wait:
            # the pid is not live any more, so the ledger must not say
            # it is. No-op after the normal path already wrote it.
            write_exit(process, None, "repair_aborted")

    # Usage-registry attribution: bind plan_id for every LLM call the
    # repair async path makes (repair generation + verification round).
    threading.Thread(
        target=_server._run_in_plan_ctx, args=(plan_id, _run), daemon=True
    ).start()
    if outcome is not None:
        outcome.handed_off = _server._plan_execution_in_flight(plan_id)
    return {"status": "started", "pid": process.pid}


#: 2026-10-23 (accident B — the repair path's ghost state): the
#: verification STATUSES that mean "this verdict ends the chain".
#:
#: 2026-09-20 (post-mortem) reached the conclusion for the convergence
#: reasons — what ends the chain is the VERDICT (the status); the stop
#: reason only DESCRIBES it.  It was never applied to the repair path,
#: so ``repair_non_zero_exit`` and the segmented
#: ``repair_batch_not_drained:<reason>:<n>_pending`` — neither of which
#: can be enumerated in a fixed reason set — left the plan parked at
#: ``current_phase='executing'`` forever: the repair round was over
#: while the plan still looked like it was running.
#:
#: A caller that is mid-loop (the executor subprocess is still
#: in-flight) says so explicitly with ``chain_ending=False``; the
#: status alone must not end the chain behind the executor's back.
_TERMINAL_VERIFICATION_STATUSES = frozenset({"passed", "failed", "loop_stopped"})


def _persist_verification_terminal(
    plan_id: str,
    status: str,
    stop_reason: str,
    *,
    chain_ending: Optional[bool] = None,
) -> None:
    """Top-level helper: persist a terminal verification outcome
    to both ``plan_verification.verification_status`` (SQLite) AND
    ``plan_routing.current_phase`` so the public /status endpoint stops
    forcing "running" on the API response.

    ``chain_ending`` overrides the "does this terminal write end the
    chain?" decision. Callers that know *by construction* that the chain
    is over (the watchdog: it only fires once the verification thread is
    dead or has stopped writing progress) pass ``True`` so a future
    addition to the stop-reason vocabulary cannot silently regress into
    a split-brain state. Callers that know the opposite — the executor
    subprocess is still mid-flight — pass ``False``; that explicit flag
    is the mid-loop guard. Leave it ``None`` to keep the default: the
    STATUS decides (see ``_TERMINAL_VERIFICATION_STATUSES``), with the
    stop-reason set kept for compatibility only (see
    ``VERIFICATION_TERMINAL_STOP_REASONS``).

    Status mapping (consistent with the inline
    ``_record_terminal`` closure in
    ``_run_auto_verification_loop``):

    * ``stop_reason`` in ``VERIFICATION_CONVERGENCE_STOP_REASONS`` →
      ``sqlite_status = "loop_stopped"`` (the auto-loop bailed
      because the same VP failed twice — successful termination
      of the auto-loop, not a per-VP failure). The test used to be
      equality against the bare spelling of the reason, so the
      ``_after_max_attempts`` variant fell through to ``"failed"``;
      see the set's definition for the full post-mortem.
    * ``status in {"passed", "failed", "loop_stopped"}`` → pass
      through unchanged.
    * anything else → ``"failed"`` (defensive default).

    Routing phase transitions (CAS ``plan_routing.current_phase`` from
    one of {``verification_running``, ``verification_repairing``}):

    * ``sqlite_status == "passed"`` → ``"completed"``
    * everything else → ``"failed"`` (re-startable from this state;
      ``/start`` accepts ``"failed"`` in its source-phases set per
      ``_EXECUTION_START_SOURCE_PHASES``).

    The function is **best-effort** — every step is wrapped in
    its own try/except so a failure on one path cannot prevent
    the other. Operators can recover manually via /stop and
    /reset_rounds if a step is persistently failing.
    """
    # 2026-09-07: Top-level entry log — operators need a single
    # identifiable line in server.log to confirm this function was
    # invoked, with caller context (status / stop_reason) for
    # cross-referencing with watchdog triggers. /force_terminal and
    # _lazy_check_verification both route through here; this log
    # lets you tell which caller fired when post-mortem-ing a
    # SIGKILL'd backend.
    _server.logger.info(
        "[verification_terminal] entry plan=%s status=%s stop_reason=%s",
        plan_id, status, stop_reason,
    )

    # ----- 1) Persist ``plan_verification.verification_status`` -----
    # Defaults so step 4 below can still mirror a terminal verdict when
    # this block raises before assigning them.
    sqlite_status = "failed"
    _round_after: Optional[int] = None
    try:
        from state_machine.repositories.verification_repository import (
            VerificationRepository as _VerifRepo,
        )
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate

        if stop_reason in _server.VERIFICATION_CONVERGENCE_STOP_REASONS:
            sqlite_status = "loop_stopped"
        elif status in {"passed", "failed", "loop_stopped"}:
            sqlite_status = status
        else:
            sqlite_status = "failed"

        _conn = _open_db(_server._state_db_path())
        try:
            _migrate(_conn)
            repo = _VerifRepo(_conn)

            # 2026-09-09 (unified card phase):
            # pre-write self-heal. If a prior partial write left the
            # row with ``results.recorded_by = "_persist_verification_terminal"``
            # but ``verification_status = "pending"`` (audit 2026-09-09),
            # the upcoming ``complete_round`` would write a fresh
            # ``results`` blob carrying the right verdict while
            # leaving the existing ``verification_status`` drift in
            # place if the underlying drift root-cause ever recurs.
            # Repair first so the row is consistent BEFORE we attempt
            # the write.
            try:
                previous = repo.repair_stale_terminal_state(plan_id)
                if previous is not None:
                    _server.logger.info(
                        "[verification_terminal] step1 pre-write repair plan=%s "
                        "previous_status=%s",
                        plan_id, previous,
                    )
            except Exception as repair_exc:
                _server.logger.warning(
                    "[verification_terminal] step1 pre-write repair failed "
                    "plan=%s: %s",
                    plan_id, repair_exc,
                )

            repo.complete_round(
                plan_id,
                {
                    "status": sqlite_status,
                    "stop_reason": stop_reason,
                    "recorded_by": "_persist_verification_terminal",
                },
                status=sqlite_status,
                stop_reason=stop_reason,
            )
            # Read the round back so step 4's mirror writes the real one
            # rather than whatever ``plan_routing.verification`` held
            # before this round ran.
            try:
                _round_after = (repo.summary(plan_id) or {}).get("round")
            except Exception:
                _round_after = None
            _server.logger.info(
                "[verification_terminal] step1 OK plan=%s sqlite_status=%s",
                plan_id, sqlite_status,
            )
        finally:
            try:
                _conn.close()
            except Exception:
                pass
    except Exception as e:
        # Persistence failure must not bubble — the in-memory
        # state is already updated and the next /status call
        # will return the right value until restart. Log so an
        # operator can spot it.
        _server.logger.exception(
            "[verification_terminal] step1 failed plan=%s: %s",
            plan_id, e,
        )

    # ----- Decide whether this terminal write ends the chain -----
    #
    # 2026-09-12 (state machine closed-loop fix): only CAS to
    # ``terminal_*`` when the chain is ACTUALLY ending. Mid-loop
    # terminations (e.g. ``_record_terminal("failed", "no_repair_tasks")``
    # called by an interim check while the executor subprocess is still
    # mid-flight) must NOT be CAS'd — otherwise the routing layer thinks
    # the plan is finished and the executor's ``transition_to(
    # "verification")`` raises Illegal transition on the next round.
    #
    # "Actually ending" = one of:
    #   - the STATUS is terminal (``passed`` / ``failed`` /
    #     ``loop_stopped``) — see the 2026-09-20 conclusion below
    #   - the caller passed ``chain_ending=True`` explicitly
    #
    # 2026-09-13: the reason set now unions
    # ``VERIFICATION_WATCHDOG_STOP_REASONS``. Previously the watchdog's
    # three stop reasons were missing, so a watchdog-terminated plan was
    # treated as "mid-loop" and BOTH step 2 (routing ``stage``) and step 3
    # (``current_phase``) were skipped — leaving
    # ``stage='verification_running'`` /
    # ``current_phase='verification_running'`` on disk while
    # ``plan_verification.verification_status='failed'``. That is exactly
    # the "飞书卡片说验证失败，本地状态还在跑" split-brain.
    #
    # 2026-10-23 (accident B — the repair path's ghost state): the
    # 2026-09-20 post-mortem reached the conclusion for the convergence
    # reasons — the verdict that ends the chain is the STATUS; the stop
    # reason only DESCRIBES it.  It had not been applied here, so the
    # repair path's ``repair_non_zero_exit`` and the segmented
    # ``repair_batch_not_drained:<reason>:<n>_pending`` (a reason string
    # no fixed set can ever enumerate) left the plan parked at
    # ``current_phase='executing'`` forever: the repair round was over
    # while the plan still looked like it was running.
    #
    # A reason whitelist is a losing game — every new stop reason has to
    # be added to it, and the one that gets forgotten strands the plan.
    # ``VERIFICATION_TERMINAL_STOP_REASONS`` therefore survives for
    # COMPATIBILITY only: it still ends the chain for a caller that
    # reports a non-terminal status carrying a terminal reason.  It is
    # no longer the classifier.
    #
    # The mid-loop guard is now the EXPLICIT ``chain_ending=False``
    # flag: a caller that knows the executor subprocess is still
    # in-flight says so, instead of relying on the absence of a reason
    # from a set that cannot enumerate segmented reasons.
    #
    # Hoisted above step 2 (it used to be computed inside step 2's ``try``,
    # so an import failure there left it unbound for step 3).
    _chain_ending = (
        chain_ending
        if chain_ending is not None
        else (
            status in _TERMINAL_VERIFICATION_STATUSES
            or stop_reason in _server.VERIFICATION_TERMINAL_STOP_REASONS
        )
    )

    # ----- 2) Transition ``plan_routing.stage`` out of "verification_*" -----
    try:
        from state_machine.repositories.routing_repository import (
            ConflictError as _RoutingConflict,
            PlanNotFoundError as _RoutingNotFound,
            RoutingRepository as _RoutingRepo,
        )
        from state_machine.db.connection import open as _open_db2
        from state_machine.db.schema import migrate as _migrate2

        _rconn = _open_db2(_server._state_db_path())
        try:
            _migrate2(_rconn)
            if _chain_ending:
                # 2026-09-17 (schema v5): the ``stage`` / ``current_phase``
                # split used to mean this CAS wrote ``terminal_done`` /
                # ``terminal_failed`` while step 3 below wrote
                # ``completed`` / ``failed`` to the *other* column — two
                # spellings of one decision, in one function, ~50 lines
                # apart. There is one column now, so both steps name the
                # same phase.
                if status == "passed":
                    _routing_target = "completed"
                else:
                    _routing_target = "failed"
                try:
                    _RoutingRepo(_rconn).try_mark_phase(
                        plan_id,
                        (
                            "verification_running",
                            "verification_repairing",
                            # ``executing`` is included so a watchdog that
                            # fires before the auto-verification loop won
                            # its own ``executing → verification`` CAS can
                            # still roll the plan out of the chain. Without
                            # it, ``stage='executing'`` + a terminal
                            # ``verification_status`` is another
                            # split-brain the dashboard cannot resolve.
                            "executing",
                        ),
                        _routing_target,
                    )
                    _server.logger.info(
                        "[verification_terminal] step2 OK plan=%s "
                        "routing_target=%s",
                        plan_id, _routing_target,
                    )
                except (_RoutingConflict, _RoutingNotFound):
                    # If the routing row is not in a verification
                    # stage (e.g. another writer already advanced
                    # it), skip the CAS — the caller's
                    # transition_to is the authoritative path.
                    _server.logger.warning(
                        "[verification_terminal] step2 skipped "
                        "(already advanced) plan=%s target=%s",
                        plan_id, _routing_target,
                    )
            else:
                # Mid-loop terminal call. Skip step 2 so the
                # executor subprocess can still complete the
                # chain. The in-memory + plan_verification write
                # (step 1) is enough for the dashboard.
                _server.logger.info(
                    "[verification_terminal] step2 SKIPPED (mid-loop) "
                    "plan=%s status=%s stop_reason=%s",
                    plan_id, status, stop_reason,
                )
        finally:
            try:
                _rconn.close()
            except Exception:
                pass
    except Exception as e:
        # Routing transition is best-effort. The next /status
        # caller will still see the in-memory + SQLite status
        # even if routing stays at "verification_running";
        # operators can also retry via /stop.
        _server.logger.exception(
            "[verification_terminal] step2 failed plan=%s: %s",
            plan_id, e,
        )

    # ----- 3) Confirm the phase + record the terminal in PlanState -----
    #
    # 2026-09-07 fix, revised by the 2026-09-17 schema-v5 collapse.
    # Before v5 this step existed to mirror step 2: step 2 CAS'd the
    # *routing* column (``terminal_failed`` / ``terminal_done``) and
    # ``current_phase`` stayed at whatever the last
    # ``PlanState._save_state_to_sqlite`` wrote (usually
    # ``verification_running``), so ``/api/plan/{id}/summary`` kept
    # reporting a running verification after the watchdog had already
    # terminated it — the "飞书卡片说验证失败，本地状态还在跑"
    # split-brain. The two columns are one column now, so step 2's CAS
    # IS the phase write and this step no longer mirrors anything.
    #
    # What is left here is PlanState bookkeeping the CAS cannot do:
    # appending to ``completed_phases`` and flipping
    # ``verification.status``. It runs ``transition_to`` (which
    # enforces legality) with ``force_set_phase`` as the
    # terminal-re-entry fall-through.
    #
    # It deliberately does NOT re-derive the target from ``status``
    # alone: ``_routing_target`` (computed above, under the same
    # ``_chain_ending`` guard) is the same phase step 2 CAS'd, so the
    # two cannot disagree. Historically this line spelled the phase
    # independently — exactly the redundancy that let them drift.
    #
    # 2026-09-12 (state machine closed-loop fix): mirror
    # step 2's ``_chain_ending`` guard. Mid-loop ``_record_terminal``
    # calls must NOT touch the phase either, because the executor
    # subprocess owns it once it transitions to ``executing`` —
    # overwriting it back to ``completed`` or ``failed`` would race
    # the executor's own writes.
    try:
        if not _chain_ending:
            _server.logger.info(
                "[verification_terminal] step3 SKIPPED (mid-loop) "
                "plan=%s status=%s stop_reason=%s",
                plan_id, status, stop_reason,
            )
        else:
            plan_dir = _server._plan_dir(plan_id)
            if plan_dir.exists():
                ps = _server.PlanState(plan_dir)
                ps.reload()
                target = _routing_target
                try:
                    ps.transition_to(target)
                except ValueError:
                    # Plan is already in a terminal state — the previous
                    # SQL UPDATE would silently overwrite current_phase
                    # here. force_set_phase makes the intent explicit.
                    ps.force_set_phase(target)
                _server.logger.info(
                    "[verification_terminal] step3 OK plan=%s "
                    "target=%s (via transition_to)",
                    plan_id, target,
                )
    except Exception as e:
        _server.logger.exception(
            "[verification_terminal] step3 failed plan=%s: %s",
            plan_id, e,
        )

    # ----- 4) Mirror the verdict into ``plan_routing.verification`` -----
    #
    # 2026-09-19: this call was missing. Step 2/3 write
    # ``plan_routing.current_phase`` (so /api/plans and the state machine
    # are correct), but ``/api/plan/{id}/summary`` serves the *separate*
    # ``verification`` JSON column — and the Feishu notifier reads
    # ``state.verification.status`` from it to pick the card header.
    # Without this, a plan that stopped its loop kept that column at
    # whatever it held before the round (observed live in that run: the
    # terminal write left it at ``pending`` from the earlier reset, so the
    # card never rendered a terminal verdict). The watchdog path has
    # called this since 2026-09-07 for exactly this reason; the auto-loop
    # termination path never did.
    #
    # It runs AFTER step 3 on purpose: step 3's PlanState write rewrites
    # the same column from ``PlanState._state``, which may still carry the
    # pre-round value.
    if _chain_ending:
        _server._update_plan_state_to_terminal(
            plan_id,
            stop_reason or "",
            status=sqlite_status,
            round_=_round_after,
        )

    # Usage accounting: a verification round just ended, so the plan's
    # session set is final for this round — re-aggregate against CC
    # Switch's ledger. Backgrounded + best-effort (see
    # ``_refresh_usage_report``); a failure here must never change the
    # verification verdict.
    _server._refresh_usage_report(plan_id)


def _count_unfinished_execution_tasks(plan_id: str) -> int:
    """Count ``plan_tasks`` rows the executor would still run
    (``status`` pending / in_progress).

    2026-09-14: used by the auto-verification dead-end disambiguation
    (``no_repair_tasks``) to decide between "roll back to execution"
    and "chain-ending terminal".  Read-only; any error is logged and
    reported as 0 so the caller falls back to the conservative
    chain-ending terminal instead of stranding the plan.
    """
    try:
        from state_machine.db.connection import open as _open_db

        conn = _open_db(_server._state_db_path())
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM plan_tasks "
                "WHERE plan_id = ? AND status IN ('pending', 'in_progress')",
                (plan_id,),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — conservative default, see docstring
        _server.logger.exception(
            "[dead_end] unfinished-task count failed plan=%s "
            "(treating as 0)", plan_id,
        )
        return 0


def _rollback_dead_end_to_queued(
    plan_id: str,
    unfinished_count: int,
    stop_reason: str,
    log_tag: str = "dead_end",
) -> bool:
    """Roll a dead-ended verification plan back to ``queued``.

    2026-09-14 when verification terminates with
    ``no_repair_tasks`` the plan must NOT strand on the user-gated
    ``verification_repairing`` phase — that gate only makes sense when
    there are repair tasks to confirm.  If execution work remains
    (``unfinished_count > 0``) the routing phase is CAS'd back to
    ``queued`` (the resumable-execution state ``/start`` accepts,
    server.py start_execution contract), so the operator can resume
    execution with an explicit ``POST /api/execution/{plan_id}/start``.
    We deliberately do NOT auto-start the executor — Ready != Start.

    2026-10-25 (R10): the target used to be spelled ``ready``.  The
    waiting phase is ``queued`` now and ``ready`` is only an INPUT
    alias of it (``backend/phase_vocabulary.py``) — the write path
    must not produce the retired word, so the function is renamed to
    match what it actually writes.

    Returns True when the rollback CAS was applied; False when the row
    was already advanced by another writer (conflict) or any error
    occurred — in both cases the caller must leave the phase alone.
    """
    try:
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.routing_repository import (
            ConflictError as _RoutingConflict,
            PlanNotFoundError as _RoutingNotFound,
            RoutingRepository as _RoutingRepo,
        )

        conn = _open_db(_server._state_db_path())
        try:
            _migrate(conn)
            _RoutingRepo(conn).try_mark_phase(
                plan_id,
                (
                    "verification_running",
                    "verification_repairing",
                ),
                "queued",
            )
        finally:
            conn.close()
    except (_RoutingConflict, _RoutingNotFound):
        _server.logger.warning(
            f"[{log_tag}] rollback skipped (stage already advanced) "
            "plan=%s", plan_id,
        )
        return False
    except Exception:  # noqa: BLE001
        _server.logger.exception(
            f"[{log_tag}] rollback CAS failed plan=%s", plan_id,
        )
        return False

    # 2026-10-16 (schema v6): the ``update_phase(plan_id, "ready")``
    # mirror that used to live here is GONE, and that is the point of
    # this change.  The routing CAS above already moved the one phase
    # column to ``ready``; the mirror existed only to keep a second
    # copy of that same value in step, and a best-effort copy that is
    # allowed to fail silently cannot be a copy worth keeping.  With
    # ``GET /api/plans`` reading the routing column there is nothing
    # left for it to reconcile.
    #
    # 2026-10-25 (R10): the phase the CAS above writes is spelled
    # ``queued`` now (it was ``ready`` when the 2026-10-16 note was
    # written) — the waiting phase was renamed and ``ready`` is now
    # only an input alias.  The mirror is still gone, and for exactly
    # the same reason.

    _server.logger.warning(
        f"[{log_tag}] plan=%s verification stopped (%s) with %d "
        "unfinished task(s) — rolled back to queued; resume "
        "with POST /api/execution/{plan_id}/start",
        plan_id, stop_reason, unfinished_count,
    )
    return True


def _round_is_pure_split(result: Dict[str, Any]) -> bool:
    """True when a round's only forward work is a set of VP splits.

    2026-09-14 : "如果他没有拆出新的修复任务，那执行状态就
    没有东西可以执行，他就直接跳过，他就会重新进入到验证状态。"

    A round that judged every failed VP "too big, split it" produces
    ``vp_splits`` and no repair tasks. Spawning the executor for that
    would run a subprocess with an empty queue — the split children are
    verified, not implemented — so the chain skips execution entirely
    and re-enters verification. A MIXED round (splits + repair tasks)
    is NOT pure: the executor has real work and runs normally, and the
    children join the next round's verification anyway.
    """
    splits = result.get("vp_splits") or []
    if not isinstance(splits, list) or not splits:
        return False
    repair = result.get("repair_tasks") or []
    return not (isinstance(repair, list) and repair)


def _dead_end_terminal(
    plan_id: str,
    reason: str,
    *,
    chain_ending: bool = True,
    rollback_to_ready: bool = True,
) -> None:
    """Terminate a verification dead-end: roll back to execution when
    work remains, otherwise close the chain.

    2026-09-14 (split-brain follow-up): a
    ``no_repair_tasks`` stop at the auto-loop's final exit is a TRUE
    terminal, not the interim mid-flight event the
    ``VERIFICATION_TERMINAL_STOP_REASONS`` design accounts for.  Two
    legitimate exits:

      * unfinished execution tasks remain  -> keep the failed
        ``plan_verification`` record, roll routing back to ``ready``
        (operator resumes execution explicitly).
      * nothing left to run               -> chain-ending terminal
        (``failed``) so the plan never strands on the user-gated
        ``verification_repairing`` phase with an empty repair list.

    ``chain_ending=False`` (2026-09-17) records the failed/dead-end
    verdict on ``plan_verification`` but does NOT close the chain. The
    auto-loop uses it on the branch that re-enters verification: writing
    a chain-ending terminal and then immediately continuing would stamp
    a terminal the plan is still working, and every later phase
    transition would be re-entering it through the ``force_set_phase``
    fallback.

    ``rollback_to_ready=False`` (2026-09-17) suppresses the
    waiting-phase (``queued``) rollback on that same branch. The
    rollback exists so an OPERATOR can resume a parked plan via ``POST
    /api/execution/{plan_id}/start``; but that endpoint's CAS accepts
    ``queued`` and has **no verification-liveness guard**, so
    leaving the row there while the auto-loop runs another verification
    round opens a window where an operator restart spawns an executor
    into a mid-flight verification. A plan that is still being worked on
    must keep its routing row inside the verification family.
    """
    final_status = "failed" if reason == "no_repair_tasks" else "loop_stopped"
    # 2026-10-23 (accident B): this call RECORDS the verdict; whether it
    # also ENDS the chain is decided further down (the rollback branch
    # below, or the explicit ``chain_ending=True`` write at the end).
    # The status-based rule would end the chain right here, so the
    # mid-loop guard has to be said out loud.
    _server._record_verification_terminal(
        plan_id, final_status, reason, chain_ending=False,
    )
    unfinished = _count_unfinished_execution_tasks(plan_id)
    if unfinished > 0:
        if rollback_to_ready:
            _rollback_dead_end_to_queued(plan_id, unfinished, reason)
        else:
            _server.logger.warning(
                "[dead_end] plan=%s has %d unfinished execution task(s); "
                "NOT rolling routing back to queued — the auto-loop "
                "re-enters verification, and queued is a phase "
                "/execution/start would CAS away underneath it",
                plan_id, unfinished,
            )
        return
    if not chain_ending:
        return
    _server._persist_verification_terminal(
        plan_id, final_status, reason, chain_ending=True,
    )


#: 2026-10-11 — independent protection counter for the undrained-repair
#: stop. Deliberately separate from ``DEFAULT_MAX_VERIFICATION_ROUNDS``
#: (which bounds verification rounds) and from
#: ``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS`` (which bounds repeats of the
#: same VP failure). This one bounds "the post-repair callback kept
#: refusing to re-verify" — the degenerate shape where repair never
#: converges and the chain would otherwise be re-dispatched forever.
_MAX_REPAIR_NOT_DRAINED_GUARDS = 3

#: 2026-10-13 — how many times a ``failed`` repair task may be revived
#: before it is abandoned for good.
#:
#: Before this, ``_get_pending_repair_tasks`` accepted ``status ==
#: 'pending'`` only, so a repair task that had failed once was dead
#: forever: nothing re-queued it, nothing counted it, nothing said so.
#: A ``repair-*`` row can reach ``failed`` without ever having run — a
#: provider chain pointed at a model that does not exist kills the first
#: attempt inside the provider, before a single tool call — and the work
#: is then buried with no counter and no log line anywhere.
#:
#: The cap is a per-row ``attempt`` budget, deliberately NOT the round
#: budget: ``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS`` bounds repeats of a
#: failure *set* and is reset when the set changes, so a single VP that
#: never converges while others flip in and out is not bounded by it.
#: Three revivals is enough for a real defect to see a second and third
#: try; past that the failure is structural, not transient.
_MAX_REPAIR_REVIVAL_ATTEMPTS = 3

#: ``{plan_id: consecutive_undrained_repair_stops}``. Module-level rather
#: than per-closure so a re-dispatched loop that rebuilds its own
#: callback still shares the count.
_repair_not_drained_guard: Dict[str, int] = {}


def _repair_path_stop_reason(plan_id: str) -> Optional[str]:
    """The stop reason the repair execution path recorded, or ``None``.

    Returns a non-``None`` value only when the in-memory execution
    record says the *repair* subprocess ran and the ``returncode == 0``
    branch decided it had unfinished tasks. A main execution run, or a
    record with no verdict yet, yields ``None`` so the caller falls
    through to its normal behaviour.
    """
    try:
        with _server._execution_state_lock:
            state = _server._execution_state.get(plan_id) or {}
            if state.get("_source") != "repair_execution":
                return None
            reason = state.get("stop_reason")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(reason, str):
        return None
    return reason or None


def _bump_repair_not_drained_guard(plan_id: str) -> int:
    """Increment and return the consecutive undrained-repair count."""
    hits = int(_repair_not_drained_guard.get(plan_id, 0)) + 1
    _repair_not_drained_guard[plan_id] = hits
    return hits


def _clear_repair_not_drained_guard(plan_id: str) -> None:
    """Reset the guard once the repair batch actually drained."""
    _repair_not_drained_guard.pop(plan_id, None)


def _repair_generation_failed(plan_id: str, error: str) -> None:
    """Record a repair-task generation FAILURE without closing the chain.

    ``result["repair_generation_error"]`` means the generator broke
    while failures were pending — the LLM call raised (provider outage,
    hard timeout) or replied with nothing usable. The verification run
    therefore produced no verdict about *what to fix*, which is NOT the
    same as "there is nothing to fix".

    The chain must not be closed in that case. We record the failure on
    ``plan_verification`` for visibility and pass
    ``chain_ending=False``, so the routing stage is not advanced by it.

    2026-10-23 (accident B): "the reason is absent from the stop-reason
    set" used to be what kept the stage put — the chain-ending decision
    was made from the reason.  It is now made from the STATUS, so a
    terminal status ends the chain unless the caller says otherwise.
    This call site is such a caller: the auto-loop retries the round,
    so the chain is demonstrably NOT over.

    Why this exists: ``generate_repair_contents`` used to swallow its
    own LLM exception and return ``[]``. The auto-loop then read the
    empty list as ``no_repair_tasks`` and terminalised — ending an
    earlier plan with three failed VPs (VP-021 / VP-034 /
    VP-036) and zero repair work, with nothing in the terminal record
    saying why.

    2026-09-17 — this no longer rolls the routing stage back to
    ``tasks_ready``, because the auto-loop now RETRIES the round instead
    of parking ( "生成失败也是要重试的，给一个重试的
    机会，跟任何的任务一样").

    The rollback had to go with it. ``tasks_ready`` is a stage
    ``POST /api/execution/{plan_id}/start`` will happily CAS to
    ``executing``, and that endpoint has no verification-liveness guard —
    so leaving the row there while the loop runs another verification
    round opened a window where an operator restart could spawn an
    executor into a plan whose verification was mid-flight. Recording
    the failure without moving the stage keeps the row inside the
    verification family, which is where the plan actually is.

    Recovery is unaffected: if the process dies mid-retry,
    ``_recover_verification_states`` restores the running round; if the
    round budget runs out, the auto-loop's tail writes a proper
    ``max_rounds_reached`` terminal.
    """
    reason = "repair_generation_failed"
    _server.logger.warning(
        "[repair_generation_failed] plan=%s repair-task generation failed "
        "(%s) — recorded; the auto-loop retries in the next round",
        plan_id, error,
    )
    _server._record_verification_terminal(
        plan_id, "failed", reason, chain_ending=False,
    )


def _round_failed_vp_ids(orch: Any, report: Any) -> set:
    """The VP ids THIS round's verdict failed, straight from the orchestrator.

    2026-10-13. The revival criterion in :func:`_get_pending_repair_tasks`
    needs the round's failure set, and it has to be the orchestrator's own
    notion of "failed" — ``_extract_failed_ids`` already knows about the
    ``timeout`` / ``SPLIT`` / ``hard_timeout`` statuses and about the
    ``rounds[-1].results`` fallback that the top-level
    ``verification_results`` list needs once a round has been snapshotted
    and cleared. Re-deriving any of that here would be a second, drifting
    definition of "failed".

    So this is a pass-through, not a new reader: it calls the orchestrator
    method the auto-loop already called two lines earlier
    (``check_cycle_conditions`` computes the same set), and degrades to
    ``None`` — revival off — if the orchestrator or the report is absent.
    Nothing here reads disk, which is the point: the round's verdict is a
    value the loop is already holding.
    """
    extractor = getattr(orch, "_extract_failed_ids", None)
    if not callable(extractor) or not isinstance(report, dict):
        return None
    try:
        return set(extractor(report) or ())
    except Exception:  # noqa: BLE001 — a revival criterion must not
        _server.logger.exception(  # abort a round that is otherwise fine
            "[repair_task_revive] could not extract failed ids plan-wide "
            "— revival disabled for this round",
        )
        return None


def _reconcile_failure_history(plan_id: str) -> Dict[str, Any]:
    """对账这个计划的失败记录，把"判据层已经判掉"的记录标记出来。

    2026-10-25（VP-031）。一个没有定义的验证点（不在
    ``verification_plan.json`` 里）会在
    ``verification_failure_history.json`` 里留下一条记录，而那条记录
    每轮都从文件里原样回来 —— 于是同一个 id 每轮都变成一条修复任务，
    而交付物那边没有任何东西可修。

    对账把这类记录标上 ``superseded_by``：**原记录保留**（审计不丢 ——
    它确实发生过，操作者要看的正是"这里有个没有定义的验证点"），但它
    不再进入失败集合。

    幂等：已经标记过的记录不再被报告一次，跑两遍结论一样。

    Returns:
        对账的摘要（``superseded`` / ``live`` / ``plan_known``），
        出错时是空 dict —— 对账失败绝不能把一轮验证带跑。
    """
    try:
        from verification import failure_history as _fh

        summary = _fh.reconcile_history(_server._plan_dir(plan_id))
    except Exception:  # noqa: BLE001 — 对账失败不是一轮失败
        _server.logger.exception(
            "[verification_reconcile] failure-history reconciliation failed "
            "plan=%s — the round continues with the records as they are",
            plan_id,
        )
        return {}

    superseded = summary.get("superseded") or []
    if superseded:
        _server.logger.info(
            "[verification_reconcile] %s failure record(s) reconciled away "
            "plan=%s — they stay in the file for the audit but no longer "
            "count as failures: %s",
            len(superseded),
            plan_id,
            ", ".join(
                f"{item.get('id')}(round {item.get('round')}, "
                f"{item.get('failure_class')})"
                for item in superseded
            ),
        )
    return summary


def _split_parent_ids(all_tasks: Dict[str, Dict[str, Any]]) -> set:
    """Ids in ``all_tasks`` that the breakdown flow has split into children.

    A breakdown writes ``f"{parent}-{n}"`` children and, depending on the
    run, either deletes the parent's row (``refiner_structure`` 2026-10-05)
    or leaves it at ``failed`` beside children that already ran. Both
    shapes are in the live database right now: one repair task has its
    row removed with its three children present, another still carries
    its own row at ``failed`` beside its children, and a third nests
    three deep (a repair task, one child, and four more of those).

    Either way the parent's *work* lives in the children. Re-queueing the
    parent would run the same fix a second time, so the caller uses this
    set to keep them out of the executor queue.

    Pure function over the rows already read; no extra query.

    **What the id-shape heuristic cannot see (2026-10-16).** This used to
    carry the claim "``{parent}-1`` is only ever a child id — no id in
    either scheme ends in a bare numeric segment that is not a breakdown
    ordinal". That claim is FALSE and this note replaces it rather than
    defending it.

    The parse is ``rpartition("-")`` + ``suffix.isdigit()``. It recovers
    the parent correctly only when the last segment really is the
    breakdown ordinal. It has no other evidence to go on: it never looks
    at ``depends_on``, which for breakdown children is usually empty in
    the live database anyway (129 of 436 rows carry a non-empty
    ``depends_on``, and breakdown children are not among them). So a last
    segment that is a bare number for any OTHER reason is read as a
    breakdown ordinal, and a parent that does not exist is manufactured.

    ``repair-r1-01`` is the live example. ``rpartition("-")`` gives
    ``("repair-r1", "01")``: the function concludes ``repair-r1`` was
    split, when ``repair-r1`` is a bare ROUND number that never exists as
    a task id. ``repair-r1`` / ``repair-r2`` / ``repair-r3`` /
    ``repair-r4`` are exactly such ghosts — read-only audit of the live
    ``state.db`` (436 rows, 5 plans, 309 distinct ids) finds 268 ids
    ending in a bare numeric segment producing 49 distinct phantom
    parents, 22 of which name a task that does not exist at all.

    **Why it is harmless today.** A phantom parent is inert unless it
    collides with a REAL task id, because the only consumer is
    ``if tid in split_parents`` over ids already read from the table. The
    audit found 27 phantom names that are also real ids, and every one of
    them is a genuine breakdown parent (they have real id-shaped children
    of their own). The bare-round shapes — the ones that would need an id
    scheme nobody uses — do not occur.

    **What would make it bite.** Any id scheme whose last segment is a
    bare integer that is NOT a breakdown ordinal, for a task id that also
    exists: a round-scoped task named ``repair-r1``, a bare ``R6`` or
    ``RP``, or a ``task_group`` value ever becoming a task id. Then
    :func:`_get_pending_repair_tasks` would classify that task as already
    split and DROP it from revival via ``repair_task_skipped_split_into_children``
    — a ``failed`` repair task whose verification point is still failing
    would silently never be offered again, while the log line reads like a
    routine breakdown decision.

    The real fix is not a smarter regex: ``depends_on`` should be written
    by the breakdown flow so the parent is known rather than inferred
    from spelling. That is a change to the breakdown writer and its
    existing rows, not to this reader, and it is deliberately NOT made
    here — this is a comment fix plus a test that pins the trap, and
    :func:`test_repair_r1_with_breakdown_child_is_silently_dropped`
    documents it as standing, not as fixed.
    """
    parents: set = set()
    for tid in all_tasks:
        if not isinstance(tid, str) or "-" not in tid:
            continue
        parent, _, suffix = tid.rpartition("-")
        if parent and suffix.isdigit():
            parents.add(parent)
    return parents


def _revive_failed_repair_task(
    plan_id: str, tid: str, entry: Dict[str, Any],
) -> bool:
    """Re-queue one ``failed`` repair task whose VP failed again.

    Returns ``True`` when the row was revived and MUST be returned by the
    caller as queued work, ``False`` when it is abandoned (at the attempt
    cap, or already split into children) — both of which are logged, never
    skipped in silence.

    2026-10-13. Three rules, each of which closes a hole the naive
    ``status == 'pending'`` filter left open:

    * **Attempt cap.** ``attempt`` is bumped and written to
      ``plan_tasks`` on every revival; a row already at
      :data:`_MAX_REPAIR_REVIVAL_ATTEMPTS` is abandoned with a
      ``repair_task_abandoned_max_attempts`` event carrying the last
      ``failure_reason``. The column was dead until now — nothing wrote
      it (``SubTask`` has no ``attempt`` field, so
      ``AutonomousAgent._persist_task_status``'s
      ``getattr(task, "attempt", None)`` was always ``None``) and all 322
      rows in the live database read ``NULL``.

    * **Split parent.** A task the breakdown flow already split has
      ``f"{tid}-{n}"`` children whose work IS this task's work. Reviving
      the parent would execute the same fix twice.

    * **Status reset.** ``status`` goes back to ``pending``, not just the
      counter. ``failed`` is in ``agent._TERMINAL_TASK_STATUSES``, so a
      row left at ``failed`` is classified terminal by the executor's
      ``_load_tasks`` Phase-2 orphan reconcile and marked ``superseded``
      without ever being scheduled — the revival would count and then do
      nothing. Flipping the status is also what makes this idempotent: the
      auto-loop calls this helper more than once per round verdict
      (``_pending`` then ``_pending_db``), and after the first call the row
      is ``pending``, so ``attempt`` advances exactly once per revival.

    ``failure_reason`` is deliberately left in place — the executor feeds
    it into the next attempt's prompt
    (``AutonomousAgent._build_prior_failure_block``), which is the whole
    point of retrying a failure rather than replaying it blind.
    """
    attempt = entry.get("attempt")
    try:
        attempt = int(attempt) if attempt is not None else 0
    except (TypeError, ValueError):
        attempt = 0
    if attempt >= _MAX_REPAIR_REVIVAL_ATTEMPTS:
        _server.logger.warning(
            "[repair_task_abandoned_max_attempts] plan=%s task=%s "
            "vp=%s attempt=%s max_attempts=%s — repair task [%s] failed "
            "and its VP is still failing, but it has already been revived "
            "%s time(s); abandoning it. It will NOT be re-queued from "
            "this row. last_failure_reason=%s",
            plan_id, tid, entry.get("failed_vp_id"), attempt,
            _MAX_REPAIR_REVIVAL_ATTEMPTS, tid, attempt,
            (entry.get("failure_reason") or "")[:500],
        )
        return False
    new_attempt = attempt + 1
    try:
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository as _PlanTaskRepo,
        )
        conn = _open_db(_server._state_db_path())
        try:
            _migrate(conn)
            _PlanTaskRepo(conn).update_task(
                plan_id, tid,
                {"status": "pending", "attempt": new_attempt},
            )
        finally:
            conn.close()
    except Exception:
        # A revival whose counter could not be persisted is not safe to
        # hand on: the row would go back to ``pending`` in this process's
        # view but stay ``failed`` on disk, and the executor would
        # classify it terminal. Report it loudly and leave the row alone.
        _server.logger.exception(
            "[repair_task_revive_failed] could not revive plan=%s task=%s "
            "attempt=%s — left as-is",
            plan_id, tid, new_attempt,
        )
        return False
    entry["status"] = "pending"
    entry["attempt"] = new_attempt
    _server.logger.warning(
        "[repair_task_revived] plan=%s task=%s vp=%s attempt=%s "
        "max_attempts=%s — repair task [%s] had failed and its VP is "
        "still failing; re-queued as attempt %s/%s. "
        "last_failure_reason=%s",
        plan_id, tid, entry.get("failed_vp_id"), new_attempt,
        _MAX_REPAIR_REVIVAL_ATTEMPTS, tid, new_attempt,
        _MAX_REPAIR_REVIVAL_ATTEMPTS,
        (entry.get("failure_reason") or "")[:500],
    )
    return True


def _is_repair_task_id(tid: str, task_group: Optional[str]) -> bool:
    """Is this task id one of the repair round's tasks?

    2026-09-12 plan v2: accept BOTH the legacy ``RP-<n>`` id format
    (pre-v9) AND the post-v9 ``R<round>-<i>`` id format. The two
    share the ``task_group`` prefix ``repair`` (and its
    ``repair-round-<N>`` variants) so we match on ``task_group``
    rather than the literal ``RP-`` prefix — which would otherwise
    silently drop R6-1 / R6-2 from the executor queue.
    See also [[ac-repair-task-persistence-2026-09-12]] for the
    parallel ``_TERMINAL_REPAIR_TASK_GROUP_PREFIX`` guard in the
    refiner at agent.py:5940.

    2026-10-23: the id shape that ``RepairTaskAssembler`` actually
    produces is LOWERCASE ``repair-r<round>-<seq>`` (``repair-r1-01``)
    and its breakdown children (``repair-r1-06-1``) — and those rows
    carry NO ``task_group``. The old predicate matched neither branch:
    ``"repair-r3-01-1"`` does not start with ``"RP-"`` nor with
    ``"R"`` (it starts with a lowercase ``r``), and its ``task_group``
    is NULL. So the "has the repair batch drained?" guard
    (:func:`_get_pending_repair_tasks`) always saw an empty list and
    never noticed a queued repair task.

    The comparison is case-insensitive so the real shape and the
    legacy ``RP-`` shape are both recognised. The predicate stays
    NARROW on purpose: a task id is a repair task when it carries a
    known repair prefix — never merely because it contains the word
    "repair" somewhere.
    """
    if not isinstance(tid, str) or not tid:
        return False
    tid_l = tid.lower()
    if tid_l.startswith("repair-") or tid_l.startswith("rp-"):
        # The real assembler shape (``repair-r3-01-1``) and the legacy
        # ``RP-<n>`` shape, case-insensitively.
        return True
    # The legacy post-v9 ``R<round>-<i>`` shape. The parentheses spell
    # out the precedence: ``and`` binds tighter than ``or``, so this
    # reads ``RP-*`` OR (``R*`` AND ``contains a dash``).
    if tid.startswith("RP-") or (tid.startswith("R") and "-" in tid):
        return True
    # Defensive fallback: even if id has an unusual prefix, the
    # orchestrator-stamped ``task_group`` is the authoritative
    # marker — ``repair*`` indicates a repair task.
    return bool(task_group) and task_group.startswith("repair")


def _get_pending_repair_tasks(
    plan_id: str,
    failed_vp_ids: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Return the repair tasks the executor must run, from ``state.db``.

    2026-09-12: regardless of whether the latest
    ``check_cycle_conditions`` produced new repair_tasks, the executor
    MUST pick up any RP-* tasks that are already pending from previous
    rounds and run them. The auto-loop previously only looked at
    ``result["repair_tasks"]`` returned by the orchestrator, which is
    empty when the orchestrator hit ``same_failure_repeated`` — leaving
    pending RP-* tasks orphaned forever and breaking the iteration
    chain.

    This helper reads ``state.db.plan_tasks`` directly and filters for
    repair-shaped ids (see :func:`_is_repair_task_id` above). Empty list
    on DB error so the caller can fall back to the orchestrator's
    ``repair_tasks`` payload.

    Parameters
    ----------
    plan_id:
        The plan whose ``plan_tasks`` rows are the queue.
    failed_vp_ids:
        The verification-point ids THIS round's verdict found failing —
        ``VerificationOrchestrator._extract_failed_ids(report)`` at the
        call sites that have a report, and ``None`` everywhere else.

        It is the sole criterion for reviving a ``failed`` row
        (2026-10-13). A repair task that failed is re-queued when — and
        only when — the VP it was written for is still in this round's
        failure set: the work was never done, so it has to be offered
        again. Pass ``None`` and the revival branch is inert, which is
        what the read-only probes want (the operator-facing reset in
        ``routes/verification.py`` and the undrained-repair guard in
        ``_on_repair_complete``, neither of which has a round verdict in
        hand).

        Rows whose ``failed_vp_id`` is NULL are never revived — there is
        no VP to test them against. The rule is unchanged by
        ``refiner_structure.inherit_failed_vp`` (2026-10-13): a breakdown
        child now inherits the VP of the repair task it was split from,
        so it can be revived at all. What still reads NULL is every child
        written before that change, and nothing backfills those — so this
        exclusion is the load-bearing rule for them, not a formality.
    """
    try:
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.plan_task_repository import (
            PlanTaskRepository as _PlanTaskRepo,
        )
        conn = _open_db(_server._state_db_path())
    except Exception:
        return []
    try:
        try:
            _migrate(conn)
        except Exception:
            conn.close()
            return []
        try:
            # Direct SQL query (rather than ``load_all``) to avoid any
            # ORM-level filtering. The helper only needs a small set
            # of columns and ``status`` is on the runtime mirror
            # (not in ``ALLOWED_STATIC_TASK_FIELDS``) — direct SQL
            # keeps the read path free of any field-filtering gotchas.
            cur = conn.execute(
                "SELECT task_id, title, description, task_group, "
                "test_command, failed_vp_id, round, status, attempt, "
                "breakdown_count, failure_reason "
                "FROM plan_tasks WHERE plan_id = ?",
                (plan_id,),
            )
            all_tasks: Dict[str, Dict[str, Any]] = {}
            for row in cur.fetchall():
                tid = row[0]
                all_tasks[tid] = {
                    "title": row[1] or "",
                    "description": row[2] or "",
                    "task_group": row[3],
                    "test_command": row[4],
                    "failed_vp_id": row[5],
                    "round": row[6],
                    "status": row[7],
                    "attempt": row[8],
                    "breakdown_count": row[9],
                    "failure_reason": row[10],
                }
        finally:
            conn.close()
    except Exception:
        return []
    # An empty / omitted failure set disables revival entirely: "this VP
    # is still failing" must be a fact, not a default.
    round_failures: set = set(failed_vp_ids) if failed_vp_ids else set()
    split_parents = _split_parent_ids(all_tasks)
    pending: List[Dict[str, Any]] = []
    for tid, entry in all_tasks.items():
        if not isinstance(entry, dict):
            continue
        if not _is_repair_task_id(tid, entry.get("task_group")):
            continue
        status = entry.get("status")
        if status != "pending":
            if status != "failed" or not round_failures:
                continue
            vp_id = entry.get("failed_vp_id")
            if not vp_id or vp_id not in round_failures:
                continue
            if tid in split_parents:
                _server.logger.warning(
                    "[repair_task_skipped_split_into_children] plan=%s "
                    "task=%s vp=%s attempt=%s children=%s — repair task "
                    "[%s] is failed but the breakdown flow already split "
                    "it; its work is in those children, so the parent is "
                    "not re-queued",
                    plan_id, tid, vp_id, entry.get("attempt"),
                    sorted(k for k in all_tasks if k.startswith(tid + "-")),
                    tid,
                )
                continue
            if not _revive_failed_repair_task(plan_id, tid, entry):
                continue
        pending.append({
            "id": tid,
            "title": entry.get("title") or "",
            "description": entry.get("description") or "",
            "failed_vp_id": entry.get("failed_vp_id"),
            "round": entry.get("round"),
            "task_group": entry.get("task_group"),
            "test_command": entry.get("test_command"),
        })
    return pending


def _handle_passed_round(
    plan_id: str,
    round_num: int,
    ps: Any,
) -> None:
    """Handle the PASSED branch of the auto-verification loop.

    2026-09-12: extracted from the inline
    ``if status == "passed":`` block in
    ``_run_auto_verification_loop`` so the same logic can be invoked
    when a round passes AND there are no pending RP-* tasks to run.

    Steps:
    1. Advance routing phase from ``verification_running`` to
       ``completed`` so the status
       endpoint reports the round as finished instead of
       ``verification_running`` (audit fix from 2026-08-19).
    2. Force-set phase to ``completed`` (with transition_to fallback)
       so a watchdog that flipped the plan to ``failed`` mid-round
       doesn't strand it. Records ``passed`` terminal.

    2026-09-17 (schema v5): steps 1 and 2 now name the same phase.
    Step 1 used to CAS the routing column to ``terminal_done`` while
    step 2 wrote ``completed`` to the phase column — one decision, two
    spellings, two columns.
    """
    try:
        from state_machine.repositories.routing_repository import (
            ConflictError as _RoutingConflict,
            PlanNotFoundError as _RoutingNotFound,
        )
        _, _routing, _ = _server._open_verification_state()
        try:
            _routing.try_mark_phase(
                plan_id,
                ("verification_running",),
                "completed",
            )
        except (_RoutingConflict, _RoutingNotFound):
            pass
    except Exception:
        pass
    try:
        ps.transition_to("completed")
    except ValueError:
        ps.force_set_phase("completed")
    _server._record_verification_terminal(plan_id, "passed", None)
    print(f"[Auto-Verification] Round {round_num}: PASSED")


def _record_verification_terminal(
    plan_id: str, status: str, stop_reason: Optional[str],
    *, chain_ending: Optional[bool] = None,
) -> None:
    """2026-09-11 plan v14: module-level wrapper that updates the
    in-memory ``_verification_state[plan_id]`` AND delegates to
    ``_persist_verification_terminal`` to advance SQLite state.

    Replaces the in-loop ``_record_terminal`` closure so unit tests
    can patch it without re-implementing closure logic. The closure
    in ``_run_auto_verification_loop`` is now a thin wrapper that
    calls this with the bound ``plan_id``.

    Side effects (observable for tests):
      * ``_verification_state[plan_id].verification_status = status``
      * ``_verification_state[plan_id].stop_reason = stop_reason``
      * ``_verification_state[plan_id].ended_at`` set to now
      * SQLite ``plan_verification`` + ``plan_routing`` advanced
        (via ``_persist_verification_terminal``)

    2026-10-23 (accident B): ``chain_ending`` is forwarded verbatim to
    ``_persist_verification_terminal``.  A mid-loop caller (the loop is
    still working and the executor subprocess is still in-flight) passes
    ``False`` — the verdict is recorded, the chain is NOT closed.
    ``None`` (the default) leaves the decision to the status-based rule
    in ``_persist_verification_terminal``.
    """
    v_state = _server._verification_state.get(plan_id)
    if v_state:
        v_state["verification_status"] = status
        v_state["stop_reason"] = stop_reason
        v_state["updated_at"] = datetime.now().isoformat()
        v_state["ended_at"] = v_state["updated_at"]

    _server._persist_verification_terminal(
        plan_id, status, stop_reason or "", chain_ending=chain_ending,
    )


# ---------------------------------------------------------------------------
# The verification-round lease (2026-10-25)
# ---------------------------------------------------------------------------
#
# One plan, one verification round.
#
# The 2026-10-24 acceptance round of the parked plan ran THREE
# overlapping rounds (21:18, 21:42, 21:45).  The 21:42 round emitted no
# ``services_ensured`` event at all and finished its nine ``api_test``
# VPs between 21:53:05 and 21:53:21 — while the 21:45 round's own
# ``services_ensured`` only completed at 21:54:08.  So one round's VPs
# ran against a service that had not been brought up yet, reading a
# fixture another round was rewriting underneath them: two rounds, two
# views of the same plan, and no single authority over either the
# fixture or ``state.db``.  That is why the progress view could not be
# trusted — "the interrupted task is still ``in_progress`` and its two
# downstream tasks are still ``pending``" is only a fact when exactly
# one round is writing.
#
# The lease is that authority.  It is a FILE, not a lock inside one
# process, because the rounds that overlap are not necessarily threads
# of one process: two backend instances (or a backend and a CLI run)
# can drive the same plan.
#
#   * the lock is created with ``O_CREAT | O_EXCL`` — the creation
#     itself is the atomic step, so two runs racing for the same plan
#     cannot both believe they won;
#   * the file records the holder's ``pid``, ``started_at`` and
#     ``heartbeat``, so a reader can tell a live holder from a corpse;
#   * a run that cannot take the lease waits a BOUNDED number of
#     seconds and then ends explicitly, with the reason
#     ``round_already_running``, without dispatching a single VP;
#   * the holder releases the lease in a ``finally``, so every exit
#     path (passed, failed, cancelled, crash) gives the plan back;
#   * a lock whose recorded pid is dead is STALE: it is broken (with a
#     log line) rather than waited on, so a killed round does not lock
#     its plan out forever.

#: The lease file's name inside the plan directory.  It is part of the
#: plan's on-disk contract: an operator who sees it knows a round owns
#: the plan, and — after checking the pid inside it — may delete it to
#: unblock a plan whose round died without releasing.
_VERIFICATION_ROUND_LOCK_FILENAME = "verification_round.lock"

#: How long a run waits for the lease before it gives up.  Bounded on
#: purpose: the alternative — waiting forever — turns a lost round into
#: a hung one, and a hung round holds the plan just as badly.
_VERIFICATION_ROUND_LEASE_WAIT_SECONDS = 30.0

#: How often the waiter re-reads the lock file while it waits.
_VERIFICATION_ROUND_LEASE_POLL_SECONDS = 0.5

#: How often the holder refreshes the recorded heartbeat.
_VERIFICATION_ROUND_LEASE_HEARTBEAT_SECONDS = 10.0

#: A lock file that carries no readable pid — what a holder killed
#: between the ``O_EXCL`` create and the write leaves behind — is only
#: broken once it is this old.  Before that, the holder may simply not
#: have written its record yet.
_VERIFICATION_ROUND_LEASE_UNREADABLE_AFTER_SECONDS = 60.0


def _verification_round_lock_path(plan_dir: Path) -> Path:
    """The path of the plan's verification-round lease file."""
    return Path(plan_dir) / _VERIFICATION_ROUND_LOCK_FILENAME


def _pid_is_alive(pid: int) -> bool:
    """Whether ``pid`` names a process that still exists.

    ``os.kill(pid, 0)`` is the cheapest question there is: it raises
    ``ProcessLookupError`` for a pid the OS has already handed back and
    succeeds for a live one.  ``PermissionError`` means the process
    exists but belongs to somebody else — alive, as far as the lease is
    concerned.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _read_verification_round_lock(path: Path) -> Optional[Dict[str, Any]]:
    """Read the lock file, or ``None`` when it carries no usable record.

    ``None`` covers three cases that mean the same thing to the caller:
    the file is unreadable, it is not JSON, or it is JSON that is not an
    object.  The difference between "absent" and "unusable" matters to
    :func:`_stale_lock_reason`, which is why the caller checks the
    file's existence separately.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _stale_lock_reason(
    path: Path, holder: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Why the lock at ``path`` is provably stale — ``None`` if it is not.

    Two ways a lock can be stale:

    * the holder's pid is gone.  That process died without running its
      ``finally`` — killed, crashed, or the machine went down — and
      nothing else will ever release the lock;
    * the file carries no pid at all and is old enough that nobody can
      still be writing it.  That is what a holder killed between the
      ``O_EXCL`` create and the write leaves behind.

    A lock whose pid is alive is NOT stale, even if the pid was reused
    by an unrelated process: the lease's contract is "a live process
    holds this", and breaking a live holder's lock is the one failure
    this mechanism must not have.
    """
    if holder is None:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return None
        if age >= _VERIFICATION_ROUND_LEASE_UNREADABLE_AFTER_SECONDS:
            return "unreadable_lock_older_than_%ds" % int(
                _VERIFICATION_ROUND_LEASE_UNREADABLE_AFTER_SECONDS,
            )
        return None
    raw_pid = holder.get("pid")
    try:
        holder_pid = int(raw_pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if holder_pid <= 0:
        return None
    if not _pid_is_alive(holder_pid):
        return "holder_pid_%d_is_dead" % holder_pid
    return None


class _VerificationRoundLease:
    """The exclusive, plan-level lease ONE verification round holds.

    The object is the holder's side of the contract:

    * :meth:`heartbeat` refreshes the recorded liveness stamp;
    * :meth:`release` gives the plan back — and it gives it back ONLY
      if this object still owns the file, so a holder that was already
      displaced never deletes the new holder's lock.
    """

    __slots__ = (
        "plan_dir", "path", "token", "pid", "started_at",
        "_released", "_stop", "_thread",
    )

    def __init__(
        self,
        plan_dir: Path,
        path: Path,
        token: str,
        pid: int,
        started_at: str,
    ) -> None:
        self.plan_dir = Path(plan_dir)
        self.path = Path(path)
        self.token = token
        self.pid = pid
        self.started_at = started_at
        self._released = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- the record on disk --------------------------------------------
    def _record(self, heartbeat: str) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_dir.name,
            "pid": self.pid,
            "started_at": self.started_at,
            "heartbeat": heartbeat,
            "token": self.token,
        }

    def holder(self) -> Dict[str, Any]:
        """What the lock file says about the holder, right now."""
        return _read_verification_round_lock(self.path) or {}

    def heartbeat(self) -> bool:
        """Refresh the recorded heartbeat.  ``False`` if the lease is gone.

        The write is in place and opens the file WITHOUT ``O_CREAT``:
        a holder that was already released must not be able to
        resurrect the lock it gave back.
        """
        if self._released:
            return False
        current = _read_verification_round_lock(self.path)
        if not current or current.get("token") != self.token:
            return False
        record = self._record(datetime.now().isoformat())
        try:
            fd = os.open(str(self.path), os.O_WRONLY | os.O_TRUNC)
        except OSError:
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh)
        except OSError:
            return False
        return True

    def start_heartbeat(
        self, interval: float = _VERIFICATION_ROUND_LEASE_HEARTBEAT_SECONDS,
    ) -> None:
        """Refresh the heartbeat in the background while the round runs."""
        if self._thread is not None:
            return

        def _beat() -> None:
            while not self._stop.wait(max(0.5, float(interval))):
                if not self.heartbeat():
                    return

        thread = threading.Thread(
            target=_beat,
            name=f"verification-round-lease-{self.plan_dir.name}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def release(self) -> None:
        """Give the plan back to the next round.

        The heartbeat thread is stopped and joined FIRST: a heartbeat
        that ran after the unlink could recreate the file it just gave
        back, and a resurrected lock is a lock nobody owns.
        """
        self._released = True
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        current = _read_verification_round_lock(self.path)
        if not current or current.get("token") != self.token:
            # Somebody else's lock (or ours is already gone) — leave it.
            return
        try:
            os.unlink(str(self.path))
        except FileNotFoundError:
            pass
        except OSError as exc:
            _server.logger.warning(
                "[verification_round_lease] could not release %s: %s",
                self.path, exc,
            )


def _acquire_verification_round_lease(
    plan_dir: Path,
    *,
    wait_seconds: float = _VERIFICATION_ROUND_LEASE_WAIT_SECONDS,
    poll_seconds: float = _VERIFICATION_ROUND_LEASE_POLL_SECONDS,
    pid: Optional[int] = None,
) -> Tuple[Optional["_VerificationRoundLease"], Optional[Dict[str, Any]]]:
    """Take the plan's verification-round lease, or say why it could not.

    Returns ``(lease, conflict)``:

    * ``(lease, None)`` — the lease is held.  The caller owns it and
      MUST :meth:`_VerificationRoundLease.release` it;
    * ``(None, None)`` — the lease file could not be created at all
      (unwritable plan directory).  The round proceeds WITHOUT
      exclusivity: the lease guards against overlapping rounds, and a
      plan directory that cannot hold a lock file is a problem the
      round hits anyway.  The failure is logged at ERROR level;
    * ``(None, conflict)`` — another live round holds the plan.
      ``conflict["reason"]`` is ``"round_already_running"`` and the
      conflict carries the holder's pid / start time / heartbeat.

    The wait is bounded: a run that cannot take the lease waits at
    most ``wait_seconds`` and then returns the conflict.  It never
    waits forever — a lost round must not become a hung one.
    """
    plan_dir = Path(plan_dir)
    path = _verification_round_lock_path(plan_dir)
    owner_pid = os.getpid() if pid is None else int(pid)
    deadline = time.monotonic() + max(0.0, float(wait_seconds))

    while True:
        token = uuid.uuid4().hex
        started_at = datetime.now().isoformat()
        record = {
            "plan_id": plan_dir.name,
            "pid": owner_pid,
            "started_at": started_at,
            "heartbeat": started_at,
            "token": token,
        }
        try:
            fd = os.open(
                str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644,
            )
        except FileExistsError:
            holder = _read_verification_round_lock(path)
            stale_reason = _stale_lock_reason(path, holder)
            if stale_reason:
                _server.logger.warning(
                    "[verification_round_lease] breaking stale lock "
                    "plan=%s path=%s reason=%s",
                    plan_dir.name, path, stale_reason,
                )
                try:
                    os.unlink(str(path))
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                holder = holder or {}
                return None, {
                    "reason": "round_already_running",
                    "plan_id": plan_dir.name,
                    "path": str(path),
                    "holder": holder,
                    "holder_pid": holder.get("pid"),
                    "started_at": holder.get("started_at"),
                    "heartbeat": holder.get("heartbeat"),
                }
            time.sleep(max(0.01, float(poll_seconds)))
            continue
        except OSError as exc:
            _server.logger.error(
                "[verification_round_lease] could not create %s: %s — the "
                "round proceeds without exclusivity", path, exc,
            )
            return None, None
        else:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(record, fh)
            except OSError as exc:
                _server.logger.error(
                    "[verification_round_lease] could not write %s: %s — "
                    "the round proceeds without exclusivity", path, exc,
                )
                try:
                    os.unlink(str(path))
                except OSError:
                    pass
                return None, None
            lease = _VerificationRoundLease(
                plan_dir, path=path, token=token, pid=owner_pid,
                started_at=started_at,
            )
            lease.start_heartbeat()
            return lease, None


def _record_round_lease_refusal(
    plan_id: str, plan_dir: Path, conflict: Dict[str, Any],
) -> None:
    """Make a refused round visible WITHOUT touching the plan's verdict.

    The run that lost the race must not write a terminal verification
    state: the round that owns the plan is still running, and a
    terminal write from the loser would park the routing row underneath
    the winner — the very cross-round interference this lease exists to
    stop.  So the refusal is recorded where it cannot hurt: the server
    log and the plan's own ``execution.log`` (the same trail the
    ``services_ensured`` event is written to).
    """
    reason = str((conflict or {}).get("reason") or "round_already_running")
    holder_pid = (conflict or {}).get("holder_pid")
    _server.logger.warning(
        "[verification_round_lease] %s plan=%s holder_pid=%s — this run "
        "dispatched no VP", reason, plan_id, holder_pid,
    )
    try:
        plan_logger = _verification_event_logger(plan_dir)
        if plan_logger is not None:
            plan_logger.warning(
                "verification_round_lease",
                f"{reason} — this run dispatched no VP",
                data={
                    "reason": reason,
                    "holder_pid": holder_pid,
                    "path": (conflict or {}).get("path"),
                },
            )
    except Exception:  # noqa: BLE001 — logging must never break a round
        pass


def _record_round_instance_handover(
    orch: Any, plan_id: str, plan_dir: Path,
) -> None:
    """Write the round's own handover into the plan's log.

    A round's address can be taken over while the round is running.
    The round knows it — it looked at the instance on both sides of
    every dispatch — but a reader who only has the round's verdict
    cannot, and the verdict is what the operator reads.

    So the round writes the two snapshots down: the instance that
    served it at the start, and the one serving it now.  The event
    lands in the plan's own ``execution.log``, next to the events the
    operator already reads for this plan, because a fact that only
    lives in the round's internals is a fact the operator cannot check.

    Best-effort: a round that cannot write its record still ran.
    """
    agent = getattr(orch, "verification_agent", None)
    handover = None
    getter = getattr(agent, "round_instance_handover", None)
    if getter is not None:
        try:
            handover = getter()
        except Exception:  # noqa: BLE001 — a record must never break a round
            handover = None
    if not isinstance(handover, dict):
        return
    before = handover.get("before")
    after = handover.get("after")
    if before is None and after is None:
        # The round never reached a service that identifies itself, so
        # there is no instance to have changed hands and nothing to say.
        # "Nobody said" is an answer, and it is not the same as
        # "somebody else said".
        return
    try:
        # The event name is the agent's own, so the two cannot drift.
        # The import is late: ``verification_agent`` is a heavy module
        # and this module is imported by the server's own wiring.
        from verification_agent import ROUND_INSTANCE_HANDOVER_EVENT

        plan_logger = _verification_event_logger(plan_dir)
        if plan_logger is not None:
            plan_logger.warning(
                ROUND_INSTANCE_HANDOVER_EVENT,
                "The instance serving this round's address at the start, "
                "and the one serving it now.",
                data={
                    "before": before,
                    "after": after,
                    "round": getattr(agent, "_current_round", None),
                },
            )
    except Exception:  # noqa: BLE001 — logging must never break a round
        pass


def _dispatch_verification_round(
    plan_id: str,
    plan_dir: Path,
    orch: Any,
    round_number: int,
    *,
    resume: bool,
    force: bool = False,
    wait_seconds: float = _VERIFICATION_ROUND_LEASE_WAIT_SECONDS,
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """Run ONE verification round while holding the plan's round lease.

    Returns ``(report, conflict)``:

    * ``(report, None)`` — the round was dispatched; ``report`` is the
      orchestrator's round report;
    * ``(None, conflict)`` — another live round owns this plan.
      ``conflict["reason"]`` is ``"round_already_running"`` and **no VP
      was dispatched**: the orchestrator was never called.

    The lease is taken around the dispatch only — the VPs run inside
    ``start_verification_cycle`` — and it is given back in a
    ``finally``, so a round that raises still returns the plan to the
    next one.

    ``wait_seconds`` is how long the run waits for the lease before it
    gives up.  It is a parameter so a caller (and a test) can ask for
    the refusal without waiting out the production window.
    """
    lease, conflict = _acquire_verification_round_lease(
        plan_dir, wait_seconds=wait_seconds,
    )
    if conflict is not None:
        return None, conflict
    try:
        # 2026-10-25 (repair-r1-33): the round gets its service
        # preflight **before** the cycle starts.  The agent calls it
        # right after it has brought the services up, and before the
        # first verification point is dispatched — so a round whose
        # services are not there spends no verification point on a
        # connection refusal.
        _install_round_service_preflight(orch, plan_id, plan_dir)
        report = orch.start_verification_cycle(
            round_number=round_number,
            force=force,
            resume=resume,
        )
    finally:
        if lease is not None:
            lease.release()
    # 2026-10-25 (repair-r1-33): a round that was blocked before it
    # dispatched anything is written down **here** — this is the one
    # place every dispatch goes through, so the refusal is recorded
    # exactly once, whichever caller asked for the round.  A record
    # only one caller wrote would leave the other without one.
    _blocked = _service_preflight_blocked_report(report)
    if _blocked is not None:
        _record_service_preflight_blocked(plan_id, plan_dir, _blocked)
    # 2026-10-26: the round's own handover — the instance that served
    # its address at the start, and the one serving it now.  It is
    # written here because this is the one place every dispatch goes
    # through, so the record is made exactly once.
    _record_round_instance_handover(orch, plan_id, plan_dir)
    return report, None


# ---------------------------------------------------------------------------
# The workflow-level round lease (VP-008, 2026-10-25)
# ---------------------------------------------------------------------------
#
# The lease above guards ONE dispatch — the window in which
# ``start_verification_cycle`` runs the VPs.  It does not guard the
# WORKFLOW: between two dispatches the loop runs its judgment phase,
# writes the round's verdict and may chain into a repair, and a second
# round of the same plan can take the dispatch lease inside that gap.
#
# On 2026-10-24 that is exactly what happened.  Three rounds of one
# plan overlapped (21:18, 21:42, 21:45); the 21:42 round emitted no
# ``services_ensured`` event at all and finished its nine ``api_test``
# VPs between 21:53:05 and 21:53:21, while the 21:45 round's own
# ``services_ensured`` only completed at 21:54:08.  One round's VPs
# therefore ran against a service that was not up yet, reading a
# fixture another round was rewriting underneath them: two rounds, two
# views of the same plan, and no single authority over either the
# fixture or ``state.db``.
#
# The workflow lease is that authority.  It is taken at the ENTRY of
# ``_run_auto_verification_loop`` — before the round's preflight,
# before the services are ensured, before a single VP is dispatched —
# and it is given back when the workflow ends.
#
# Its ``round_token`` is the round's SERVICE OWNER token: the same
# token the ledger stamps on the services the round starts and the
# same token the round's pin (VP-004) carries.  One round, one token,
# one authority over the services it uses.

#: The workflow lease's file inside the plan directory.  It is hidden
#: (dot-prefixed) because it is machinery, not plan content: an
#: operator browsing the plan directory should see the plan's own
#: artefacts, not the lease.  It is DISTINCT from the dispatch-level
#: lock above (``verification_round.lock``): that one guards a single
#: ``start_verification_cycle``, this one guards the whole workflow.
_VERIFICATION_WORKFLOW_LEASE_FILENAME = ".verification_round.lock"

#: How long a run waits for the lease before it gives up.  Zero by
#: default: the loser of the race must not hold its caller.  The plan
#: is being verified by somebody else; this run's job is to say so and
#: end, not to stand in front of the plan for half a minute.
_VERIFICATION_WORKFLOW_LEASE_WAIT_SECONDS = 0.0

#: How often the waiter re-reads the lease file while it waits.
_VERIFICATION_WORKFLOW_LEASE_POLL_SECONDS = 0.25

#: How often the holder refreshes the recorded heartbeat.
_VERIFICATION_WORKFLOW_LEASE_HEARTBEAT_SECONDS = 10.0

#: A lease whose heartbeat is older than this is treated as dead: the
#: holder stopped saying it was alive, so the round behind it is gone
#: (wedged, killed, its thread died) and the next round may take the
#: plan over.  The margin over the heartbeat interval is wide on
#: purpose — a false takeover puts two rounds back on one plan, which
#: is the failure this lease exists to prevent.
_VERIFICATION_WORKFLOW_LEASE_STALE_SECONDS = 120.0

#: How many stale leases one run may break before it gives up and
#: proceeds without exclusivity.  A lease that keeps looking stale and
#: keeps surviving the unlink is being written by somebody else; the
#: run must not spin on it.
_VERIFICATION_WORKFLOW_LEASE_MAX_STALE_BREAKS = 3

#: The event the refused round writes: it lost the race.
ROUND_LEASE_DENIED_EVENT = "round_lease_denied"

#: The event that marks the round BLOCKED because of the conflict.
#: The same name the verification agent uses for an environment-blocked
#: round: the question a log reader asks is "was this round about the
#: product or about the plumbing", and the answer is a property of the
#: round, not of one verification point.
ROUND_BLOCKED_EVENT = "verification_round_blocked"

#: Why the round was blocked.
ROUND_CONFLICT_ERROR_KIND = "round_conflict"


def _verification_workflow_lease_path(plan_dir: Path) -> Path:
    """The path of the plan's workflow-level round lease."""
    return Path(plan_dir) / _VERIFICATION_WORKFLOW_LEASE_FILENAME


def _read_verification_workflow_lease(
    path: Path,
) -> Optional[Dict[str, Any]]:
    """Read the workflow lease, or ``None`` when it carries no record.

    ``None`` covers three cases that mean the same thing to the caller:
    the file is unreadable, it is not JSON, or it is JSON that is not
    an object.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _workflow_lease_heartbeat_age(
    holder: Optional[Dict[str, Any]], path: Path,
) -> Optional[float]:
    """How long ago the holder last said it was alive, in seconds.

    The recorded heartbeat is the answer; the file's mtime is the
    fallback for a lease whose record is unreadable — a holder killed
    between the ``O_EXCL`` create and the write leaves exactly that.
    """
    if holder:
        raw = holder.get("heartbeat_at")
        if isinstance(raw, str) and raw:
            try:
                moment = datetime.fromisoformat(raw)
            except ValueError:
                moment = None
            if moment is not None:
                return max(0.0, (datetime.now() - moment).total_seconds())
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _workflow_lease_stale_reason(
    path: Path, holder: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Why the workflow lease at ``path`` is provably stale — ``None`` if not.

    Two ways a lease can be stale:

    * the holder's pid is gone — the process died without running its
      ``finally``, and nothing else will ever release the lease;
    * the heartbeat went stale — the holder stopped refreshing it, so
      the round behind it is wedged or dead.

    A lease whose pid is alive AND whose heartbeat is fresh is NOT
    stale, even if the pid was reused by an unrelated process: the
    lease's contract is "a live round holds this", and displacing a
    live holder is the one failure this mechanism must not have.
    """
    if holder is None:
        age = _workflow_lease_heartbeat_age(None, path)
        if (
            age is not None
            and age >= _VERIFICATION_WORKFLOW_LEASE_STALE_SECONDS
        ):
            return "unreadable_lease_older_than_%ds" % int(
                _VERIFICATION_WORKFLOW_LEASE_STALE_SECONDS,
            )
        return None
    raw_pid = holder.get("pid")
    try:
        holder_pid = int(raw_pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        holder_pid = 0
    if holder_pid > 0 and not _pid_is_alive(holder_pid):
        return "holder_pid_%d_is_dead" % holder_pid
    age = _workflow_lease_heartbeat_age(holder, path)
    if age is not None and age >= _VERIFICATION_WORKFLOW_LEASE_STALE_SECONDS:
        return "heartbeat_stale_for_%ds" % int(age)
    return None


class _VerificationWorkflowLease:
    """The exclusive, plan-level lease ONE verification workflow holds.

    The holder's side of the contract:

    * :meth:`heartbeat` refreshes the recorded liveness stamp;
    * :meth:`release` gives the plan back — and gives it back ONLY if
      this object still owns the file, so a holder that was already
      displaced never deletes the new holder's lease.
    """

    __slots__ = (
        "plan_dir", "path", "round_token", "run_id", "pid", "started_at",
        "_released", "_stop", "_thread",
    )

    def __init__(
        self,
        plan_dir: Path,
        path: Path,
        round_token: str,
        run_id: str,
        pid: int,
        started_at: str,
    ) -> None:
        self.plan_dir = Path(plan_dir)
        self.path = Path(path)
        self.round_token = round_token
        self.run_id = run_id
        self.pid = pid
        self.started_at = started_at
        self._released = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- the record on disk --------------------------------------------
    def _record(self, heartbeat_at: str) -> Dict[str, Any]:
        return {
            "round_token": self.round_token,
            "run_id": self.run_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "heartbeat_at": heartbeat_at,
        }

    def holder(self) -> Dict[str, Any]:
        """What the lease file says about the holder, right now."""
        return _read_verification_workflow_lease(self.path) or {}

    def heartbeat(self) -> bool:
        """Refresh the recorded heartbeat.  ``False`` if the lease is gone.

        The write opens the file WITHOUT ``O_CREAT``: a holder that
        was already released must not be able to resurrect the lease it
        gave back.
        """
        if self._released:
            return False
        current = _read_verification_workflow_lease(self.path)
        if not current or current.get("round_token") != self.round_token:
            return False
        record = self._record(datetime.now().isoformat())
        try:
            fd = os.open(str(self.path), os.O_WRONLY | os.O_TRUNC)
        except OSError:
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh)
        except OSError:
            return False
        return True

    def start_heartbeat(
        self, interval: float = _VERIFICATION_WORKFLOW_LEASE_HEARTBEAT_SECONDS,
    ) -> None:
        """Refresh the heartbeat in the background while the round runs."""
        if self._thread is not None:
            return

        def _beat() -> None:
            while not self._stop.wait(max(0.5, float(interval))):
                if not self.heartbeat():
                    return

        thread = threading.Thread(
            target=_beat,
            name=f"verification-workflow-lease-{self.plan_dir.name}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def release(self) -> None:
        """Give the plan back to the next round.

        The heartbeat thread is stopped and joined FIRST: a heartbeat
        that ran after the unlink could recreate the file it just gave
        back, and a resurrected lease is a lease nobody owns.
        """
        self._released = True
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        current = _read_verification_workflow_lease(self.path)
        if not current or current.get("round_token") != self.round_token:
            # Somebody else's lease (or ours is already gone) — leave it.
            return
        try:
            os.unlink(str(self.path))
        except FileNotFoundError:
            pass
        except OSError as exc:
            _server.logger.warning(
                "[verification_workflow_lease] could not release %s: %s",
                self.path, exc,
            )


def _acquire_verification_workflow_lease(
    plan_dir: Path,
    *,
    round_token: str = "",
    run_id: str = "",
    wait_seconds: float = _VERIFICATION_WORKFLOW_LEASE_WAIT_SECONDS,
    poll_seconds: float = _VERIFICATION_WORKFLOW_LEASE_POLL_SECONDS,
    pid: Optional[int] = None,
) -> Tuple[Optional["_VerificationWorkflowLease"], Optional[Dict[str, Any]]]:
    """Take the plan's workflow-level round lease, or say why it could not.

    Returns ``(lease, conflict)``:

    * ``(lease, None)`` — the lease is held.  The caller owns it and
      MUST :meth:`_VerificationWorkflowLease.release` it;
    * ``(None, None)`` — the lease file could not be created at all
      (an unwritable plan directory).  The workflow proceeds WITHOUT
      exclusivity: the lease guards against overlapping rounds, and a
      plan directory that cannot hold a lease file is a problem the
      round hits anyway.  The failure is logged at ERROR level;
    * ``(None, conflict)`` — another live round holds the plan.
      ``conflict["reason"]`` is ``"round_conflict"`` and the conflict
      carries the holder's record.

    The wait is bounded: a run that cannot take the lease waits at
    most ``wait_seconds`` and then returns the conflict.  It never
    waits forever — a lost round must not become a hung one.
    """
    plan_dir = Path(plan_dir)
    path = _verification_workflow_lease_path(plan_dir)
    owner_pid = os.getpid() if pid is None else int(pid)
    token = str(
        round_token or f"round-{owner_pid}-{uuid.uuid4().hex[:12]}",
    )
    run = str(run_id or plan_dir.name)
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    stale_breaks = 0

    while True:
        started_at = datetime.now().isoformat()
        record = {
            "round_token": token,
            "run_id": run,
            "pid": owner_pid,
            "started_at": started_at,
            "heartbeat_at": started_at,
        }
        try:
            fd = os.open(
                str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644,
            )
        except FileExistsError:
            holder = _read_verification_workflow_lease(path)
            stale_reason = _workflow_lease_stale_reason(path, holder)
            if stale_reason:
                stale_breaks += 1
                _server.logger.warning(
                    "[verification_workflow_lease] breaking stale lease "
                    "plan=%s path=%s reason=%s",
                    plan_dir.name, path, stale_reason,
                )
                if stale_breaks > _VERIFICATION_WORKFLOW_LEASE_MAX_STALE_BREAKS:
                    _server.logger.error(
                        "[verification_workflow_lease] could not take %s — "
                        "the workflow proceeds without exclusivity", path,
                    )
                    return None, None
                try:
                    os.unlink(str(path))
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                held = holder or {}
                return None, {
                    "reason": ROUND_CONFLICT_ERROR_KIND,
                    "plan_id": plan_dir.name,
                    "path": str(path),
                    "holder": held,
                    "holder_pid": held.get("pid"),
                    "holder_round_token": held.get("round_token"),
                    "holder_run_id": held.get("run_id"),
                    "holder_started_at": held.get("started_at"),
                    "holder_heartbeat_at": held.get("heartbeat_at"),
                }
            time.sleep(max(0.01, float(poll_seconds)))
            continue
        except OSError as exc:
            _server.logger.error(
                "[verification_workflow_lease] could not create %s: %s — "
                "the workflow proceeds without exclusivity", path, exc,
            )
            return None, None
        else:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(record, fh)
            except OSError as exc:
                _server.logger.error(
                    "[verification_workflow_lease] could not write %s: %s — "
                    "the workflow proceeds without exclusivity", path, exc,
                )
                try:
                    os.unlink(str(path))
                except OSError:
                    pass
                return None, None
            lease = _VerificationWorkflowLease(
                plan_dir, path=path, round_token=token, run_id=run,
                pid=owner_pid, started_at=started_at,
            )
            lease.start_heartbeat()
            return lease, None


def _record_round_lease_denied(
    plan_id: str, plan_dir: Path, conflict: Dict[str, Any],
) -> None:
    """Write the ``round_lease_denied`` event: this run lost the race.

    The event names the holder, because "somebody else is verifying
    this plan" and "the plan is broken" are different problems and an
    operator has to be able to tell them apart.
    """
    reason = str((conflict or {}).get("reason") or ROUND_CONFLICT_ERROR_KIND)
    holder = (conflict or {}).get("holder") or {}
    _server.logger.warning(
        "[verification_workflow_lease] %s plan=%s holder_pid=%s — this run "
        "dispatched no VP", reason, plan_id,
        (conflict or {}).get("holder_pid"),
    )
    try:
        plan_logger = _verification_event_logger(plan_dir)
        if plan_logger is not None:
            plan_logger.warning(
                ROUND_LEASE_DENIED_EVENT,
                f"{reason} — another verification round owns this plan; "
                f"this run dispatched no VP",
                data={
                    "reason": reason,
                    "plan_id": plan_id,
                    "holder": holder,
                    "holder_pid": (conflict or {}).get("holder_pid"),
                    "holder_round_token": (conflict or {}).get(
                        "holder_round_token",
                    ),
                    "path": (conflict or {}).get("path"),
                },
            )
    except Exception:  # noqa: BLE001 — logging must never break a round
        pass


def _mark_round_blocked_by_round_conflict(
    plan_id: str, plan_dir: Path, conflict: Dict[str, Any],
) -> None:
    """Mark the round BLOCKED(round_conflict).

    The round that lost the race measured NOTHING: it ensured no
    service, seeded no fixture and dispatched no VP.  So the round is
    blocked, not failed — there is no verdict to repair against, and a
    failed verdict would send the repair chain after a product that was
    never looked at.

    The mark goes into the plan's own execution log, next to the
    ``round_lease_denied`` event.  It does NOT go into the shared
    in-memory verification state: the round that owns the plan is
    still running, and a loser that overwrote that state would park
    the plan's routing row underneath the winner — the very cross-round
    interference this lease exists to stop.
    """
    holder = (conflict or {}).get("holder") or {}
    reason = (
        "another verification round owns this plan "
        f"(holder pid {(conflict or {}).get('holder_pid')}, "
        f"round token {(conflict or {}).get('holder_round_token')})"
    )
    try:
        plan_logger = _verification_event_logger(plan_dir)
        if plan_logger is not None:
            plan_logger.warning(
                ROUND_BLOCKED_EVENT,
                f"{ROUND_CONFLICT_ERROR_KIND}: {reason}",
                data={
                    "plan_id": plan_id,
                    "status": "BLOCKED",
                    "error_kind": ROUND_CONFLICT_ERROR_KIND,
                    "reason_code": ROUND_CONFLICT_ERROR_KIND,
                    "reason": reason,
                    "holder": holder,
                },
            )
    except Exception:  # noqa: BLE001 — logging must never break a round
        pass


# ---------------------------------------------------------------------------
# The round's service preflight (2026-10-25, repair-r1-33)
# ---------------------------------------------------------------------------
#
# A round whose services are not there does not fail fast: it spends one
# verification point per service the plan names, and every one of them is
# graded on a connection refusal.  The refusal reads as "the project
# answered wrong", the round reports FAILED verdicts, and the repair
# chain goes after a product nobody ever looked at.
#
# So the round asks **before** it dispatches anything whether the
# services it is about to use are actually there.  The question belongs
# here (the loop owns the round); the facts belong to the verification
# agent (it owns the services, their addresses and the identity each
# one is expected to answer with).  The two meet through a hook the
# dispatch installs and the agent calls.
#
# The gate is closed by default: a service that cannot be shown to be
# usable blocks the round.  A round that is blocked has measured
# nothing, so it reports no verdict — a FAILED verdict would be a
# statement about a product that was never asked.

#: The event the blocked round writes: the round's services were not
#: usable, so the round dispatched nothing.
PREFLIGHT_FAILED_EVENT = "preflight_failed"

#: Why the round was blocked.
SERVICE_PREFLIGHT_ERROR_KIND = "service_preflight_failed"


def _install_round_service_preflight(
    orch: Any, plan_id: str, plan_dir: Path,
) -> bool:
    """Give the round its service preflight.

    The agent owns the round's services; the loop owns the question
    "may this round dispatch its verification points at all?".  The
    agent calls the hook right after it has brought the services up,
    and before the first verification point is dispatched.

    Returns ``True`` when the hook was installed.  A round whose
    orchestrator carries no agent runs without a preflight: the gate
    is a safety net, not a prerequisite, and a legacy path must not
    stop verifying because of it.
    """
    agent = getattr(orch, "verification_agent", None)
    setter = getattr(agent, "set_round_service_preflight", None)
    if setter is None:
        return False
    try:
        setter(lambda: _run_service_preflight(agent, plan_id, plan_dir))
    except Exception:  # noqa: BLE001 — a round without a gate still runs
        return False
    return True


def _run_service_preflight(
    agent: Any, plan_id: str, plan_dir: Path,
) -> Optional[Dict[str, Any]]:
    """Ask whether the round's services may be used at all.

    Every service the round is bound to is looked at three times over:

      1. a TCP connection to the port — is anything serving there;
      2. the health URL, asked **with the request guard's header**, so
         the probe travels the same road a verification point's request
         travels;
      3. the ``instance`` block the service publishes about itself,
         compared with the identity the round recorded when it started
         it — is the thing answering the round's own instance.

    A service that fails any of the three is re-ensured **once** and
    looked at again: a service that died after the round started it
    can come back.  A service that is still not there after that ends
    the round — the return value is the failure record the round is
    blocked with, naming the service, the port, the process and the
    reason.
    """
    try:
        targets = agent.service_preflight_targets()
    except Exception as exc:  # noqa: BLE001 — a round that cannot read
        # its own services cannot establish that they are usable.
        return {
            "service": "",
            "port": 0,
            "pid": None,
            "error_kind": "preflight_error",
            "detail": (
                "the round could not read its own services: "
                f"{type(exc).__name__}: {exc}"
            ),
        }

    for target in targets:
        failure = _probe_service_for_preflight(agent, target)
        if failure is None:
            continue
        if not failure.pop("start_attempted", False):
            # The service was reachable but not usable — the round's
            # one re-ensure is spent here.  When the **port** was dead,
            # the shared precondition has already spent it: it is the
            # one that brings a service back, and it asked the same
            # question this gate asks.
            outcome = _reensure_preflight_service(agent, target)
            if not outcome.get("restored"):
                failure["detail"] = (
                    f"{failure['detail']}; the re-ensure did not bring the "
                    f"service back ({outcome.get('detail') or 'no detail'})"
                )
                return failure
            # The service may have **moved**: a re-ensure that could not
            # place the service on its old port puts it somewhere else, so
            # the round asks the agent again where the service is.
            try:
                fresh = agent.service_preflight_targets()
            except Exception as exc:  # noqa: BLE001 — defensive
                fresh = []
                failure["detail"] = (
                    f"{failure['detail']}; the round could not re-read its "
                    f"services after the re-ensure: {exc}"
                )
            moved = next(
                (
                    t for t in fresh
                    if str(t.get("service")) == str(target.get("service"))
                ),
                None,
            )
            if moved is not None:
                failure = _probe_service_for_preflight(agent, moved)
                if failure is None:
                    continue
        failure["detail"] = (
            f"{failure['detail']}; the service is still not usable after "
            f"the re-ensure"
        )
        return failure

    # 2026-10-26: the round's services are usable — but is the round
    # asking the **instance** it thinks it is asking?
    #
    # On 2026-10-26 a verification point was refused a connection on port
    # 64469 at 02:25:01, and the round could not say whether the refusal
    # was about the product or about a service that had been replaced
    # behind its back.  The criterion the round was verifying was never
    # looked at, because the round never learned who was answering it.
    #
    # So the round names the instance before it dispatches anything: the
    # **epoch** — the pid and the boot id of the process serving the
    # round's own address.  A round that talks to no service has no
    # instance to name, so there is nothing to enforce.  A round that
    # talks to a service and cannot name the instance behind it would be
    # reading somebody else's data, so it dispatches nothing.
    if targets:
        epoch = _round_instance_epoch(agent)
        if epoch is not None and not epoch:
            return {
                "service": "",
                "port": 0,
                "pid": None,
                "error_kind": _epoch_unresolved_error_kind(),
                "detail": (
                    "the round could not name the instance serving its "
                    "address, so it cannot say whose answers it would be "
                    "reading; the round dispatched nothing"
                ),
                "start_attempted": False,
            }

    try:
        plan_logger = _verification_event_logger(plan_dir)
        if plan_logger is not None:
            plan_logger.info(
                "service_preflight",
                f"the round's services are usable: {len(targets)} checked",
                data={"plan_id": plan_id, "services": len(targets)},
            )
    except Exception:  # noqa: BLE001 — logging must never break a round
        pass
    return None


def _epoch_unresolved_error_kind() -> str:
    """The error kind a round blocked by an unnamed instance carries."""
    try:
        from verification_agent import EPOCH_UNRESOLVED_ERROR_KIND
    except Exception:  # pragma: no cover - defensive
        return "instance_epoch_unresolved"
    return EPOCH_UNRESOLVED_ERROR_KIND


def _round_instance_epoch(agent: Any) -> Optional[Dict[str, Any]]:
    """The instance the round is asking — named by the agent.

    The agent owns the addresses, so it owns the answer to "who is
    serving them".  The epoch is read over ``/health/data`` — the same
    road a verification point's request travels — so the round's idea of
    who is answering is the service's own statement about itself.

    Returns:
        ``None`` — the agent knows nothing about epochs (a legacy
        orchestrator): the round runs the way it has always run, and
        nothing is enforced.
        ``{}`` — the agent knows about epochs and **could not name the
        instance**: an answer nobody can attribute says nothing about
        the product, so the round must not dispatch.
        A dict — the named instance.
    """
    establish = getattr(agent, "round_instance_epoch", None)
    if not callable(establish):
        establish = getattr(agent, "establish_instance_epoch", None)
    if not callable(establish):
        # A round whose agent does not know about epochs (a legacy
        # orchestrator) is a round that has always run the way it always
        # ran.
        return None
    try:
        epoch = establish()
    except Exception as exc:  # noqa: BLE001 - a broken reader blocks
        _server.logger.warning(
            "[verification_preflight] could not read the round's instance "
            "epoch: %s", exc,
        )
        return {}
    return dict(epoch) if isinstance(epoch, dict) else {}


def _ensure_service_ready_for_preflight(
    agent: Any, target: Dict[str, Any],
) -> Dict[str, Any]:
    """Ask the round's **one** precondition about a preflight target.

    The target names the service and the port; the agent knows the
    declaration.  The agent's own method is used, so the start goes
    through the round's adoption gates — the address the round's
    verification points are dispatched to follows the binding, so a
    service that came back on another port is reached **there**.

    This is the same function the per-verification-point gate uses
    (see ``verification_agent.Orchestrator.ensure_vp_services_ready``).
    Two gates that ask the question differently are two gates that can
    disagree, and the 2026-10-26 round dispatched twelve verification
    points at a service its own gate had already refused.
    """
    service = str(target.get("service") or "")
    try:
        port = int(target.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    declaration = None
    getter = getattr(agent, "service_declaration", None)
    if callable(getter):
        try:
            declaration = getter(service)
        except Exception:  # noqa: BLE001 — defensive
            declaration = None
    if declaration is None:
        return {
            "ready": False,
            "error_kind": "no_declaration",
            "detail": f"the round has no declaration for service {service!r}",
            "start_attempted": False,
        }
    try:
        outcome = agent.ensure_service_ready(declaration)
    except Exception as exc:  # noqa: BLE001 — a broken gate blocks
        return {
            "ready": False,
            "error_kind": "preflight_error",
            "detail": (
                "the round's service precondition raised: "
                f"{type(exc).__name__}: {exc}"
            ),
            "start_attempted": False,
        }
    if not isinstance(outcome, dict):
        return {
            "ready": False,
            "error_kind": "preflight_error",
            "detail": "the round's service precondition returned no result",
            "start_attempted": False,
        }
    return outcome


def _probe_service_for_preflight(
    agent: Any, target: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Look at one service; return the failure, or ``None`` when it is usable.

    The readiness question — is the service actually serving? — is asked
    by the **one** precondition every verification path shares
    (:func:`verification_agent.ensure_service_ready`): the same port
    probe, the same start attempt and the same HTTP self-check.

    What is left here is the question only the **round** can answer:
    is the instance answering the round's own?
    """
    service = str(target.get("service") or "")
    try:
        port = int(target.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    pid = target.get("pid")
    if not port:
        return {
            "service": service,
            "port": 0,
            "pid": pid,
            "error_kind": "tcp_connect_failed",
            "detail": f"the round has no address for the service {service!r}",
            "start_attempted": False,
        }
    outcome = _ensure_service_ready_for_preflight(agent, target)
    if not outcome.get("ready"):
        return {
            "service": service,
            "port": port,
            "pid": pid,
            "error_kind": str(
                outcome.get("error_kind") or "tcp_connect_failed"
            ),
            "detail": str(outcome.get("detail") or ""),
            "start_attempted": bool(outcome.get("start_attempted")),
        }
    observed = agent.probe_service_instance(port)
    mismatch = agent.service_identity_mismatch(
        service, port, observed=observed,
    )
    if mismatch is None:
        return None
    reasons = mismatch.get("reasons") or []
    return {
        "service": service,
        "port": port,
        "pid": pid,
        "error_kind": "identity_mismatch",
        "detail": (
            "the instance answering is not the round's own: "
            + ("; ".join(reasons) if reasons else "the instance does not match")
        ),
        "mismatched_fields": list(mismatch.get("mismatched_fields") or []),
        "start_attempted": False,
    }


def _reensure_preflight_service(
    agent: Any, target: Dict[str, Any],
) -> Dict[str, Any]:
    """Spend the round's one re-ensure on a service that is not there.

    The re-ensure is the project's own start path — the same one the
    round's start-up used — and its result goes through the same
    adoption gates.  A re-ensure that fails is not retried: a round
    that kept trying would spend a start attempt per verification
    point, which is the very thing this gate exists to prevent.
    """
    service = str(target.get("service") or "")
    port = int(target.get("port") or 0)
    try:
        outcome = agent.reensure_preflight_service(service, port)
    except Exception as exc:  # noqa: BLE001 — a failed re-ensure blocks
        return {
            "restored": False,
            "port": port,
            "pid": None,
            "detail": (
                f"the re-ensure of {service!r} raised: "
                f"{type(exc).__name__}: {exc}"
            ),
        }
    if not isinstance(outcome, dict):
        return {
            "restored": False,
            "port": port,
            "pid": None,
            "detail": f"the re-ensure of {service!r} returned no result",
        }
    return outcome


def _record_service_preflight_blocked(
    plan_id: str, plan_dir: Path, failure: Dict[str, Any],
) -> None:
    """Write the round's refusal down: the service was not usable.

    The event names the service, the port, the process and the reason
    — the four facts an operator needs to act on the round — and the
    round-blocked mark beside it says what the refusal means: the
    round measured nothing, so it has no verdict about the product.
    """
    failure = failure or {}
    service = str(failure.get("service") or "")
    port = failure.get("port")
    error_kind = str(failure.get("error_kind") or "")
    detail = str(failure.get("detail") or "")
    where = f"the service {service!r}" if service else "the round's services"
    if port:
        where = f"{where} on port {port}"
    reason = f"{where} was not usable: {error_kind or 'unknown reason'}"
    _server.logger.warning(
        "[verification_service_preflight] %s plan=%s — this round "
        "dispatched no VP", reason, plan_id,
    )
    try:
        plan_logger = _verification_event_logger(plan_dir)
        if plan_logger is None:
            return
        plan_logger.warning(
            PREFLIGHT_FAILED_EVENT,
            f"{SERVICE_PREFLIGHT_ERROR_KIND}: {reason} — "
            f"this round dispatched no VP",
            data={
                "plan_id": plan_id,
                "status": "BLOCKED",
                "service": service,
                "port": port,
                "pid": failure.get("pid"),
                "error_kind": error_kind,
                "detail": detail,
                "reason_code": SERVICE_PREFLIGHT_ERROR_KIND,
                "reason": reason,
            },
        )
        plan_logger.warning(
            ROUND_BLOCKED_EVENT,
            f"{SERVICE_PREFLIGHT_ERROR_KIND}: {reason}",
            data={
                "plan_id": plan_id,
                "status": "BLOCKED",
                "error_kind": SERVICE_PREFLIGHT_ERROR_KIND,
                "reason_code": SERVICE_PREFLIGHT_ERROR_KIND,
                "reason": reason,
            },
        )
    except Exception:  # noqa: BLE001 — logging must never break a round
        pass


def _service_preflight_blocked_report(
    report: Any,
) -> Optional[Dict[str, Any]]:
    """The failure a blocked round carries, or ``None``.

    A report whose ``stop_reason`` is ``service_preflight_failed``
    carries the failure the round was blocked with, so the loop can
    write it down and end the round without a verdict.
    """
    if not isinstance(report, dict):
        return None
    if str(report.get("stop_reason") or "") != SERVICE_PREFLIGHT_ERROR_KIND:
        return None
    failure = report.get(SERVICE_PREFLIGHT_ERROR_KIND)
    if not isinstance(failure, dict):
        return {
            "service": "",
            "port": 0,
            "pid": None,
            "error_kind": "",
            "detail": str(report.get("summary") or ""),
        }
    return failure


# ---------------------------------------------------------------------------
# The base-drift hand-off (2026-10-29)
# ---------------------------------------------------------------------------
#
# A round the base-drift check blocked dispatched nothing: the branch
# had drifted away from its base and merging the base back left
# conflicts.  Such a round has no verdict to give — its report is
# empty, and an empty report that reaches ``check_cycle_conditions``
# reads as "everything failed", which sends the repair chain after a
# product nobody ever looked at.
#
# What the loop does with such a round depends on what the check left
# behind:
#
#   * no repair task — the conflict was not turned into work, so the
#     round ends here and says why;
#   * a repair task — the conflict went through the repair channel that
#     already exists, so the round hands the plan to the executor, the
#     way every other repair round does.

#: The ``stop_reason`` a round blocked by a base-drift conflict
#: carries.  It is the base-drift check's own event name for the
#: ``conflicts`` status — the stopped round and the event log say the
#: same word.
BASE_DRIFT_CONFLICTS_STOP_REASON = base_drift.EVENT_BY_STATUS[
    base_drift.STATUS_CONFLICTS
]

#: The base-drift status that blocks a round.
BASE_DRIFT_CONFLICTS_STATUS = base_drift.STATUS_CONFLICTS


def _base_drift_blocked_report(
    report: Any,
) -> Optional[Dict[str, Any]]:
    """The conflict a base-drift-blocked round carries, or ``None``.

    A report whose ``stop_reason`` is ``base_drift_conflicts`` says the
    round never dispatched a verification point: the branch had drifted
    away from its base, and merging the base back left conflicts.  The
    conflict's own evidence travels with the report — which base was
    merged, at which commit, and which files are in conflict — so the
    loop can write it down and end the round without a verdict.

    Only that one ``stop_reason`` counts.  A round that ran, a round
    blocked by its services, a round that simply failed: none of them
    is this function's business.
    """
    if not isinstance(report, dict):
        return None
    if str(report.get("stop_reason") or "") != BASE_DRIFT_CONFLICTS_STOP_REASON:
        return None
    conflicts = report.get("base_drift_conflicts")
    blocked = dict(conflicts) if isinstance(conflicts, dict) else {}
    if not blocked.get("status"):
        blocked["status"] = BASE_DRIFT_CONFLICTS_STATUS
    return blocked


def _base_drift_round_action(report: Any) -> Optional[str]:
    """What the loop does with a base-drift-blocked round.

    ``"stop"`` — the conflict was not turned into a repair task, so the
    round ends here and the reason is written down.  ``"repair"`` — the
    conflict became a task through the repair channel that already
    exists, so the round hands the plan to the executor.  ``None`` —
    this report is not a base-drift report at all.

    A task id that is empty, blank or missing is **no** task: there is
    nothing to repair with, and a repair dispatch with nothing behind
    it would send the executor after an empty queue.
    """
    if _base_drift_blocked_report(report) is None:
        return None
    task_id = (report.get("base_drift_repair_task_id") or "").strip()
    return "repair" if task_id else "stop"


class _VerificationLoopOutcome:
    """Whether the loop ended the workflow or handed it off.

    The verification workflow has two kinds of exit, and before
    2026-09-23 every caller conflated them:

    * **Terminal** — the loop reached a verdict (passed / failed /
      loop_stopped / orchestrator-init failure / exception) and nothing
      will resume it. The plan's services are no longer needed and the
      routing phase on disk is the truth.
    * **Hand-off** — the loop dispatched a background repair execution
      (:func:`_run_repair_execution_async`) and will resume from its
      ``on_complete`` callback. The workflow is still running: the
      repair round needs the plan's services, and the ``executing``
      phase the dispatcher just wrote is the truth rather than a stale
      value to be promoted.

    Conflating them produced two live defects. The plan was stamped
    ``completed`` the moment its repair round *started* — so the Feishu
    card read ``⏸ 暂停（上游阻塞）`` while ``repair-r1-01`` was visibly
    running on the same card (a production plan,
    2026-09-23 18:46) — and ``_reap_managed_services`` tore down the
    dev servers that repair round was about to use.

    The assumption was true when ``_run_repair_execution`` blocked
    inline. It stopped being true in the 2026-09-11 v14 refactor that
    made the dispatch async, and no caller was updated.

    A third exit arrived with the workflow-level round lease
    (2026-10-25, VP-008): **refused**.  Another live round owns the
    plan, so this run dispatched nothing at all and ended.  It is not
    terminal — the plan's verification workflow IS still running, it
    is just not THIS run — and the difference matters: a terminal
    outcome lets the caller promote an ``executing`` plan to
    ``completed``, which would park the plan underneath the round that
    actually owns it.
    """

    __slots__ = ("handed_off", "refused")

    def __init__(self) -> None:
        self.handed_off = False
        self.refused = False

    @property
    def terminal(self) -> bool:
        """True iff no background execution will resume this workflow."""
        return not (self.handed_off or self.refused)


def _record_dispatch_bindings(
    plan_dir: Path, plan_id: str, *, reason: str = "",
) -> None:
    """Write down the addresses this round dispatched its VPs to.

    The last link of the address chain (2026-10-25, repair-r1-25).  The
    ledger says which address a service ended up on; the earlier events
    say how it got there; this says the round actually sent its traffic
    there.  On 2026-10-24 a request went to port 51725 and nothing on
    disk said where that number came from — the whole point of the
    stream is that the question has an answer.

    The round's own ledger is the authority: it is written by the
    project's copy of ``service_manager`` and read here from the same
    plan directory, which is the only state the two share.

    Never raises — a log that cannot be written is not worth losing a
    round over.
    """
    try:
        import service_manager
    except Exception:  # noqa: BLE001 - observability must not break a round
        return
    try:
        ledger = service_manager.read_ledger(Path(plan_dir))
        entries = ledger.get("services")
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            port = entry.get("port")
            if not isinstance(port, int):
                continue
            service_manager.record_address_event(
                plan_dir, "dispatch_bound",
                run_id=plan_id or Path(plan_dir).name,
                round_token=str(entry.get("owner_token") or ""),
                old_port=None, new_port=port,
                declared_port=entry.get("declared_port") or port,
                pid=entry.get("pid"),
                boot_id=service_manager.boot_id_of(
                    entry.get("pid"), entry.get("start_epoch"),
                ),
                reason=reason or (
                    f"the round dispatched to service "
                    f"{entry.get('name')!r} on port {port}"
                ),
            )
    except Exception:  # noqa: BLE001 - never break a round over a log
        _server.logger.warning(
            "[service_manager] could not record dispatch bindings for "
            "plan=%s", plan_id, exc_info=True,
        )


def _run_auto_verification_loop(plan_id: str, plan_dir: Path, project_dir: Path, max_rounds: int = _server.DEFAULT_MAX_VERIFICATION_ROUNDS, tool: Optional[str] = None, start_round: int = 1) -> _VerificationLoopOutcome:
    """Thin lifecycle wrapper around :func:`_run_auto_verification_loop_inner`.

    The inner loop has a dozen ``return`` paths (passed, failed,
    loop_stopped, orchestrator-init failure, exception) and one normal
    fall-through. Reaping in a ``finally`` here covers **all** of them
    without threading a cleanup call through each — and "the workflow
    exited" is exactly the moment the plan's services stop being
    needed.

    2026-09-23: "a dozen return paths" turned out to be thirteen. The
    repair-dispatch paths return while a background execution is still
    running, so ``finally`` must ask the loop *how* it ended rather
    than assume every return is terminal. Callers get the same answer
    back via the returned :class:`_VerificationLoopOutcome` — they used
    to re-derive it from ``PlanState.get_current_phase()``, which is
    how the plan got stamped ``completed`` mid-repair.

    2026-10-24: the loop now owns a *token* for its round. The ledger
    records which round started each service, so this loop's exit reaps
    the services this round started and leaves alone the ones a newer,
    still-running round owns. Before the token existed, one ledger per
    plan directory was enough to make an older round's exit stop the
    newer round's api server 47 seconds after it came up — before that
    round's first api_test asked it for ``/health``. The token is
    registered on entry (so the services this round starts are stamped
    with it) and released before the reap (so the round is over by the
    time its own services are stopped).

    2026-10-25 (VP-008): the loop now takes the plan's WORKFLOW-level
    round lease here, at the entry of the workflow — before the round's
    preflight, before the services are ensured, before a single VP is
    dispatched. On 2026-10-24 three rounds of one plan overlapped
    (21:18, 21:42, 21:45), so one round's VPs ran against a service
    that was not up yet while another round rewrote the fixture
    underneath them. A round that cannot take the lease ends here,
    explicitly: it records ``round_lease_denied``, marks its round
    BLOCKED(round_conflict) and dispatches nothing. The lease's
    ``round_token`` IS this loop's service-owner token, so the lease,
    the ledger and the round's pin (VP-004) all name the same round.
    """
    outcome = _VerificationLoopOutcome()
    owner_token = _server._new_service_owner_token("verification_loop")
    lease, conflict = _acquire_verification_workflow_lease(
        plan_dir,
        round_token=owner_token or (
            f"verification_loop-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        ),
        run_id=plan_id,
    )
    if conflict is not None:
        # Another live round owns this plan. This run ensures nothing,
        # dispatches no VP and touches no state the winner owns.
        _record_round_lease_denied(plan_id, plan_dir, conflict)
        _mark_round_blocked_by_round_conflict(plan_id, plan_dir, conflict)
        outcome.refused = True
        return outcome
    _server._register_service_owner(plan_dir, owner_token, current=True)
    try:
        _server._run_auto_verification_loop_inner(
            plan_id, plan_dir, project_dir,
            max_rounds=max_rounds, tool=tool, start_round=start_round,
            outcome=outcome,
        )
    finally:
        _server._release_service_owner(plan_dir, owner_token)
        if lease is not None:
            lease.release()
        if outcome.terminal:
            _server._reap_managed_services(
                plan_id, plan_dir, "verification_loop_exit",
                owner_token=owner_token,
            )
        else:
            _server.logger.info(
                "[service_manager] reap deferred plan=%s — a repair "
                "execution is still in flight, the workflow has not "
                "exited (reason=verification_loop_exit)",
                plan_id,
            )
    return outcome


def _settle_phase_after_verification_loop(
    plan_dir: Path, outcome: _VerificationLoopOutcome,
) -> None:
    """Give a still-``executing`` plan a resting phase after the loop.

    Both callers of :func:`_run_auto_verification_loop` end with the same
    three lines — "if the loop didn't move ``plan_state``, and the plan
    is still ``executing``, call it ``completed``". That fallback exists
    for real cases (a loop that bailed before its terminal write, a
    mocked loop in tests).

    It must NOT fire on a hand-off. The repair dispatcher sets
    ``executing`` microseconds before the loop returns, so the fallback
    used to read "the loop returned, phase is executing, therefore the
    plan is done" and stamp ``completed`` on a plan whose repair round
    had just started.

    Extracted from the two call sites so the distinction is one
    testable decision instead of two copied guards (2026-09-23).

    A ``None`` outcome means the caller did not get one back — the loop
    was stubbed or replaced (several suites monkeypatch
    ``_run_auto_verification_loop`` with a bare ``lambda``). That is not
    a hand-off, and crashing the executor watcher over it would be a
    worse regression than the one this guard fixes, so ``None`` keeps
    the pre-existing "assume it ended" behaviour.
    """
    if outcome is not None and not outcome.terminal:
        return
    ps = _server.PlanState(plan_dir)
    if ps.get_current_phase() == "executing":
        ps.transition_to("completed")


# ---------------------------------------------------------------------------
# What a round snapshot has to carry (2026-10-23)
# ---------------------------------------------------------------------------
#
# The intent gate (R4) reports the repair rows it dropped on the round's
# *result* — ``intent_conflicts`` / ``needs_operator_decision``. That
# result lives in memory for the length of one round: neither
# ``verification_repair_tasks.json`` nor the in-memory
# ``_verification_state`` entry ever learned about it, so a conflict was
# gone the moment the round ended, and a server restart erased the only
# trace an operator could have read.
#
# Two helpers, used by BOTH write sites — the repair-resume callback
# (:func:`_make_repair_on_complete`) and the auto-loop
# (:func:`_run_auto_verification_loop_inner`) — so the snapshot cannot
# drift between the two paths that write it.


def _repair_intent_round_fields(result: Any) -> Dict[str, Any]:
    """Extract the intent-gate facts a round snapshot has to carry.

    Returns ``{"intent_conflicts": [...], "summary":
    {"needs_operator_decision": bool}}``.

    Nothing here raises. A result that is not a dict, or one from a round
    that predates the gate, carries no conflicts: the snapshot says so
    explicitly (``[]`` / ``False``) rather than guessing. The flag is
    True when the round reported a conflict — and the result's own
    ``needs_operator_decision`` is honoured as well, so a decision the
    round asked for is never dropped on the way to the file.
    """
    conflicts: List[Dict[str, Any]] = []
    flagged = False
    if isinstance(result, dict):
        raw = result.get("intent_conflicts")
        if isinstance(raw, (list, tuple)):
            # A conflict the gate could not describe is not a conflict
            # an operator can act on — but the ones it could describe
            # are copied, not aliased, so the snapshot cannot change
            # under the writer's feet.
            conflicts = [dict(c) for c in raw if isinstance(c, dict)]
        raw_flag = result.get("needs_operator_decision")
        if raw_flag is None:
            # A snapshot read back as a "result" carries the flag inside
            # ``summary`` — accept that shape too.
            summary = result.get("summary")
            if isinstance(summary, dict):
                raw_flag = summary.get("needs_operator_decision")
        flagged = bool(raw_flag)
    return {
        "intent_conflicts": conflicts,
        "summary": {"needs_operator_decision": bool(conflicts) or flagged},
    }


def _build_repair_round_entry(
    round_number: Any,
    tasks: Any,
    result: Any,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one round entry for ``verification_repair_tasks.json``.

    The entry is what the round accumulation and the card readers see:
    ``round`` / ``generated_at`` / ``tasks``, plus the intent facts
    (``intent_conflicts`` and the ``summary`` flag). A ``tasks`` value
    that is not a list is dropped — the aggregation walks
    ``entry["tasks"]`` as a list, and a snapshot it cannot read is worse
    than one that admits it has no tasks.
    """
    if not isinstance(tasks, list):
        tasks = []
    entry: Dict[str, Any] = {
        "round": round_number,
        "generated_at": generated_at or datetime.now().isoformat(),
        "tasks": list(tasks),
    }
    entry.update(_repair_intent_round_fields(result))
    return entry


def _make_repair_on_complete(
    plan_id: str,
    project_dir: Path,
    tool: Optional[str] = None,
) -> Callable[[int], None]:
    """Build the callback that re-enters verification after a repair.

    2026-10-22: the callback used to be a closure nested inside
    :func:`_run_auto_verification_loop_inner`. It became a factory so
    that the auto-loop and the ``/start`` endpoint's repair-resume path
    share ONE implementation of the post-repair chain — the resume path
    is the same chain, and a second copy of it is a second thing that
    can drift.

    The body below is the closure's body, unchanged: only the binding
    moved. ``_record_terminal`` is the same helper the loop had — it is
    ``_server._record_verification_terminal`` for this plan.
    """

    def _record_terminal(status: str, stop_reason: str):
        """Persist a terminal verification outcome for this plan.

        The loop's own ``_record_terminal`` (a closure over the same
        two arguments) does exactly this; it is re-bound here because
        this factory must not reach into the loop's scope.
        """
        _server._record_verification_terminal(plan_id, status, stop_reason)

    def _on_repair_complete(returncode: int) -> None:
        """Re-enter verification after repair execution finishes.

        Fires from the background thread spawned by
        :func:`_run_repair_execution_async`. Leaves ``max_rounds``
        alone (it is the plan's immutable budget since 2026-09-15;
        it used to be bumped by 1 per re-entry, which meant the cap
        never bound), stops the chain once the next round would
        exceed it, CASes routing.stage back to ``verification``,
        invokes a fresh ``start_verification_cycle`` with
        ``resume=True`` to skip already-PASSED VPs, then branches on
        the result:
        still failing → another async repair; passed →
        ``_record_terminal("passed", None)``; loop_stopped →
        ``_record_terminal("loop_stopped", stop_reason)``.

        Why a fresh orchestrator instance: ``_previous_failed_ids``
        is checked at the start of every ``check_cycle_conditions``
        call to detect ``same_failure_repeated``. A new
        orchestrator resets that state, which is correct after
        a real executor pass — the prior failures may
        genuinely be fixed and we shouldn't auto-loop-stop on
        the fresh set's first occurrence.

        2026-10-11: a fourth branch, checked first — if the repair
        path recorded ``repair_exited_with_unfinished_tasks:N``
        and repair work is still queued, the chain stops with
        ``loop_stopped`` instead of starting another
        verification round. Bounded by its own
        :data:`_MAX_REPAIR_NOT_DRAINED_GUARDS` counter, which is
        unrelated to the round budget: this bounds "repair never
        drains", not "too many verification rounds".
        """
        _server.logger.info(
            "[Auto-Verification] Repair execution finished plan=%s "
            "rc=%d — re-entering verification",
            plan_id, returncode,
        )
        # 2026-09-15: if the operator stopped the
        # plan while this repair was running, do NOT re-enter
        # verification — the chain would restart work the operator
        # explicitly halted (and re-stamp the parked routing row).
        try:
            import verification_cancel as _vc_mod2
            if _vc_mod2.is_cancelled(plan_id):
                _server.logger.warning(
                    "[Auto-Verification] stop requested — not "
                    "re-entering verification plan=%s", plan_id,
                )
                return
        except Exception:  # noqa: BLE001
            pass

        # ------------------------------------------------------------------
        # 2026-10-11 — do not re-verify over an undrained repair batch.
        #
        # ``_run_repair_execution_async`` now cross-checks the runtime
        # overlay before calling a 0 return code a completion (see the
        # ``repair_exited_with_unfinished_tasks`` branch). This is the
        # consumer of that fact. Previously the return code was turned
        # into one log line and dropped, and the callback went on to
        # ``start_verification_cycle(..., resume=True)`` no matter
        # what — so a round whose repair executor bailed out at the
        # first micro-layer was immediately followed by a fresh
        # verification of code that was never repaired. Each such round
        # re-measures the same untouched tree, so the VP it
        # re-measures is guaranteed to fail again and buy the
        # next one — a loop that spends its whole round budget
        # without ever touching the defect.
        #
        # Two signals must agree before we stop, so a benign
        # bookkeeping hiccup cannot strand a plan:
        #   1. the repair path recorded an unfinished-task stop
        #      reason, AND
        #   2. ``_get_pending_repair_tasks`` still finds queued
        #      repair work.
        # ------------------------------------------------------------------
        _repair_stop_reason = _repair_path_stop_reason(plan_id)
        if _repair_stop_reason:
            _still_pending = _get_pending_repair_tasks(plan_id)
            if _still_pending:
                _guard_hits = _bump_repair_not_drained_guard(plan_id)
                if _guard_hits >= _MAX_REPAIR_NOT_DRAINED_GUARDS:
                    _reason = (
                        f"repair_never_drained:"
                        f"{_repair_stop_reason}:"
                        f"{_guard_hits}x"
                    )
                else:
                    _reason = (
                        f"repair_batch_not_drained:"
                        f"{_repair_stop_reason}:"
                        f"{len(_still_pending)}_pending"
                    )
                _server.logger.error(
                    "[Auto-Verification] repair round did not "
                    "drain — NOT starting the next verification "
                    "round plan=%s: %d repair task(s) still "
                    "pending (repair stop_reason=%s, guard hit "
                    "%d/%d). Re-verifying now would only "
                    "re-measure unrepaired code and buy a known "
                    "FAIL.",
                    plan_id, len(_still_pending),
                    _repair_stop_reason, _guard_hits,
                    _MAX_REPAIR_NOT_DRAINED_GUARDS,
                )
                _record_terminal("loop_stopped", _reason)
                return
            # The overlay disagrees with the reason string — the
            # queue is empty after all. Reset the guard and fall
            # through to the normal re-entry.
            _clear_repair_not_drained_guard(plan_id)
        else:
            _clear_repair_not_drained_guard(plan_id)

        try:
            # 2026-09-12 (state machine closed-loop fix):
            # ``executing → verification`` is the proper
            # state-machine transition for the post-repair
            # re-entry. ``begin_verification`` (plan_state.py:884+)
            # handles idempotency and bookkeeping. Pass
            # ``round_n=_next_round`` so the bumped round is
            # preserved (the executor completed round N; the
            # next verification must start at round N+1, not
            # round 0 — the original ``begin_verification``
            # signature clobbered the round counter to 0 which
            # broke the monotonic invariant).
            # 2026-09-13: ``open_db`` and ``migrate`` are
            # NOT in scope at the callback's enclosing closure
            # (imports are localised to other helper functions
            # in this file). Import here so the callback
            # doesn't NameError on the very first line that
            # touches the DB.
            from state_machine.db.connection import open as open_db
            from state_machine.db.schema import migrate as _migrate
            plan_dir = _server._plan_dir(plan_id)
            if plan_dir.exists():
                ps = _server.PlanState(plan_dir)
                ps.reload()  # pick up executor's state writes
                # Will be overwritten below once _next_round
                # is computed, so begin_verification with
                # default round_n=0 is safe.
                ps.begin_verification()

            # 2026-09-15: ``max_rounds`` is the
            # plan's BUDGET and this path must not touch it.
            #
            # It used to do ``min(_cur_max + 1, 10)`` on every
            # re-entry — "so the chain has one more iteration of
            # headroom". The effect was that the cap never bound:
            # ``_next_round`` was computed as ``round + 1`` and the
            # cap grew in lockstep, so a plan whose rounds kept
            # failing could never reach ``max_rounds_reached``. The
            # a production plan showed the fingerprint (``plan_state.json``
            # said 4, ``plan.db`` said 5). The cap must not be
            # raised on every iteration.
            #
            # So: keep the cap exactly as the plan was set up with,
            # and when the next round would exceed it, stop the
            # chain the same way the main loop does. Removing only
            # the bump would have been worse than leaving it — the
            # "still failing → spawn next repair" branch below has
            # no ceiling check of its own, so the chain would have
            # re-entered round N+1 forever.
            _v_conn = open_db(_server._state_db_path())
            try:
                _migrate(_v_conn)
                from state_machine.repositories.verification_repository import (
                    VerificationRepository as _VR2,
                )
                _vr = _VR2(_v_conn)
                _cur = _vr.current(plan_id)
                (
                    _next_round,
                    _new_max,
                    _round_budget_exhausted,
                ) = _plan_next_round(_cur)
                if not _round_budget_exhausted:
                    _vr.init_round(
                        plan_id,
                        round_n=_next_round,
                        max_rounds=_new_max,
                    )
            finally:
                _v_conn.close()

            if _round_budget_exhausted:
                _server.logger.warning(
                    "[Auto-Verification] round budget exhausted "
                    "plan=%s — round %d would exceed max_rounds=%d; "
                    "stopping the chain (the cap is immutable "
                    "since 2026-09-15)",
                    plan_id, _next_round, _new_max,
                )
                _record_terminal(
                    "loop_stopped", "max_rounds_reached",
                )
                return

            # 2026-09-12: replace the raw routing CAS
            # (lines 6223-6260 below in the OLD version) with
            # ``begin_verification(round_n=_next_round)`` so
            # the post-repair re-entry goes through the
            # proper state-machine edge
            # ``executing → verification``. ``begin_verification``
            # is a no-op when the plan is already in a
            # ``verification_*`` state (idempotent re-entry),
            # so this is safe even when the executor subprocess
            # raced ahead and CAS'd the phase to
            # ``verification``.
            if plan_dir.exists():
                ps = _server.PlanState(plan_dir)
                ps.reload()
                ps.begin_verification(round_n=_next_round)

            # Invoke a fresh verification round.
            try:
                _coding_tool = _server.create_coding_tool(
                    tool, cwd=str(project_dir), scene="verification",
                    logger=_verification_event_logger(plan_dir),
                )
                _verif_repo = None
                try:
                    _vc, _vr2, _vv = _server._open_verification_state()
                    _verif_repo = _vv
                    # 2026-09-15 FIX — do NOT close ``_vc`` here.
                    # The repository is BOUND to this connection;
                    # closing it left the freshly-started round
                    # writing through a dead handle (see
                    # ``_bind_verification_state_conn``).
                    _bind_verification_state_conn(plan_id, _vc)
                except Exception:
                    _verif_repo = None
                _new_orch = _server.VerificationOrchestrator(
                    plan_dir, project_dir,
                    coding_tool=_coding_tool,
                    verif_repo=_verif_repo,
                )
                # 2026-09-15 FIX — flip the in-memory entry to
                # ``running`` BEFORE the cycle starts. This
                # callback only wrote the result AFTER the round
                # finished, so for the entire duration of the
                # re-entered round the map still held the
                # PREVIOUS round's terminal status: /api/system/
                # active reported ``total_active: 0`` and the card
                # showed the old verdict while VPs were actively
                # running (observed live through round 2).
                _mark_verification_round_running(
                    plan_id, _next_round,
                )
                _report, _round_conflict = _dispatch_verification_round(
                    plan_id, plan_dir, _new_orch, _next_round,
                    resume=True,
                )
                if _round_conflict is not None:
                    # 2026-10-25: another live round owns this plan, so
                    # this re-entry dispatched no VP.  The repair chain
                    # stops here; the refusal is recorded in the plan's
                    # execution log and the round that owns the plan
                    # keeps its verdict.
                    _record_round_lease_refusal(
                        plan_id, plan_dir, _round_conflict,
                    )
                    return
                # 2026-10-25 (repair-r1-33): the round's services were
                # not usable, so the round dispatched nothing and
                # measured nothing.  The judgment phase must not run on
                # it — it would read the blocked round as a failure and
                # send the repair chain after a product nobody ever
                # looked at, round after round, until the budget ran
                # out.  The chain ends here; the refusal was written
                # down by the dispatch.
                if _service_preflight_blocked_report(_report) is not None:
                    return
                # 2026-10-30 (VP-012): the round was blocked by the
                # base-drift check — the branch had drifted away from
                # its base and merging the base back left conflicts.
                # The round dispatched nothing, so its report is empty:
                # sending it to ``check_cycle_conditions`` would read
                # the block as a failure and send the repair chain
                # after a product nobody ever looked at.  This is the
                # SECOND judgment point — the auto-loop's own round is
                # the first — and the blocked round is owed the same
                # treatment here.
                _base_drift_blocked = _base_drift_blocked_report(_report)
                if _base_drift_blocked is not None:
                    if _base_drift_round_action(_report) == "repair":
                        # The conflict went through the repair channel
                        # that already exists, so the round hands the
                        # plan to the executor the way every other
                        # repair round does.  The callback is the one
                        # that is running — this scope has no loop
                        # outcome to hand over, and a second callback
                        # would be a second post-repair chain.
                        #
                        # The work is a BASE MERGE, not a product-code
                        # fix, so the executor runs under the merge's own
                        # provider-routing scene.  The scene reaches the
                        # child process through the environment.
                        _server._run_repair_execution_async(
                            plan_id, project_dir, tool=tool,
                            on_complete=_on_repair_complete,
                            scene=base_drift.BASE_DRIFT_MERGE_SCENE,
                        )
                        return
                    # No repair task behind the conflict: there is
                    # nothing to repair with, so the round ends here
                    # and says why.
                    _server.logger.warning(
                        "[Auto-Verification] repair callback: round "
                        "blocked by base drift — no repair task to run; "
                        "stopping plan=%s",
                        plan_id,
                    )
                    _record_terminal(
                        "loop_stopped", BASE_DRIFT_CONFLICTS_STOP_REASON,
                    )
                    return
                # The round has run against the addresses the ledger
                # names — write them down (2026-10-25, repair-r1-25).
                _record_dispatch_bindings(
                    plan_dir, plan_id,
                    reason=(
                        f"round {_next_round} dispatched to the addresses "
                        f"its services were bound to"
                    ),
                )
                _result = _new_orch.check_cycle_conditions(
                    _report, round_number=_next_round,
                )

                # Persist the result into the in-memory
                # ``_verification_state`` so /progress reads
                # it. This runs on a background thread, not
                # the FastAPI request thread, so we use the
                # lock-protected dict mutation path.
                with _server._verification_lock:
                    _v_state = _server._verification_state.setdefault(
                        plan_id, {},
                    )
                    _v_state["verification_round"] = _next_round
                    _v_state["verification_status"] = _result.get(
                        "status", "running",
                    )
                    _v_state["repair_tasks"] = _result.get(
                        "repair_tasks", [],
                    )
                    _v_state["stop_reason"] = _result.get(
                        "stop_reason",
                    )
                    # 2026-09-14: keep the split record on the
                    # in-memory state too — the card reads
                    # ``vp_splits`` from the progress payload.
                    _v_state["vp_splits"] = (
                        _result.get("vp_splits") or []
                    )
                    # 2026-10-23: the intent gate's verdict belongs on
                    # the state as well — a conflict that only lives in
                    # the round's result is gone once the round ends.
                    _intent_fields = _repair_intent_round_fields(
                        _result,
                    )
                    _v_state["intent_conflicts"] = (
                        _intent_fields["intent_conflicts"]
                    )
                    _v_state["needs_operator_decision"] = (
                        _intent_fields["summary"][
                            "needs_operator_decision"
                        ]
                    )
                    _v_state["updated_at"] = (
                        datetime.now().isoformat()
                    )

                # Persist repair_tasks.json to disk so the
                # next server restart sees the new round /
                # status. Mirrors the
                # _run_auto_verification_loop body at
                # server.py:5516.
                try:
                    _rt_payload = _result.get(
                        "repair_tasks", [],
                    ) or []
                    if not isinstance(_rt_payload, list):
                        _rt_payload = []
                    # 2026-10-23: the round entry is built by the same
                    # helper the auto-loop uses, so the intent facts
                    # (conflicts + the operator-decision flag) land on
                    # the snapshot no matter which path wrote it.
                    _rt_entry = _build_repair_round_entry(
                        _next_round, _rt_payload, _result,
                    )
                    _rt_path = (
                        _server._plan_dir(plan_id)
                        / "verification_repair_tasks.json"
                    )
                    _rt_path.parent.mkdir(
                        parents=True, exist_ok=True,
                    )
                    with _rt_path.open(
                        "w", encoding="utf-8",
                    ) as _rtf:
                        json.dump(
                            {
                                "plan_id": plan_id,
                                **_rt_entry,
                            },
                            _rtf,
                            ensure_ascii=False,
                            indent=2,
                        )
                except Exception:
                    _server.logger.exception(
                        "[repair_async] repair_tasks.json write "
                        "failed plan=%s",
                        plan_id,
                    )

                # Branch on result.
                # 2026-09-12: also check DB for pending
                # RP-* tasks — same  as the
                # main loop fix. If executor produced new
                # tasks OR DB has pending tasks from prior
                # rounds, run the executor.
                _new_repair = _result.get("repair_tasks") or []
                _pending_in_db = _get_pending_repair_tasks(
                    plan_id,
                    _round_failed_vp_ids(_new_orch, _report),
                )
                if _pending_in_db and not _new_repair:
                    _server.logger.info(
                        "[Auto-Verification] Post-repair round "
                        "%d: orchestrator returned %d new tasks "
                        "but %d pending in DB — running DB queue",
                        _next_round, len(_new_repair),
                        len(_pending_in_db),
                    )
                    _new_repair = _pending_in_db
                if _new_repair:
                    _server.logger.info(
                        "[Auto-Verification] Post-repair round %d "
                        "still failing plan=%s — spawning next "
                        "repair",
                        _next_round, plan_id,
                    )
                    # 2026-10-22: the operator stopped the plan while
                    # this round was running. The repair tasks are
                    # already written, but dispatching the executor now
                    # would restart work the operator explicitly halted
                    # — the same reason the round-boundary check above
                    # skips the judgment / repair chain.
                    import verification_cancel as _vc_dispatch
                    if _vc_dispatch.is_cancelled(plan_id):
                        _server.logger.warning(
                            "[Auto-Verification] stop requested — not "
                            "dispatching repair plan=%s", plan_id,
                        )
                        return
                    _server._run_repair_execution_async(
                        plan_id, project_dir, tool=tool,
                        on_complete=_on_repair_complete,
                    )
                elif _result.get("status") == "passed":
                    _record_terminal("passed", None)
                elif _result.get("status") == "loop_stopped":
                    _record_terminal(
                        "loop_stopped",
                        _result.get("stop_reason"),
                    )
                elif _round_is_pure_split(_result):
                    # 2026-09-14: this round's only
                    # forward work is a VP split — nothing for the
                    # executor to do, so chain straight into the next
                    # verification round, which runs the children.
                    # Guarded by the round cap so a
                    # split→verify→split chain cannot recurse forever.
                    if _next_round >= _new_max:
                        _server.logger.warning(
                            "[Auto-Verification] pure split at the "
                            "round cap (%d/%d) — stopping; split "
                            "children were not verified",
                            _next_round, _new_max,
                        )
                        _record_terminal(
                            "loop_stopped", "max_rounds_reached",
                        )
                    else:
                        _server.logger.warning(
                            "[Auto-Verification] Post-repair round %d "
                            "is a pure split — re-entering "
                            "verification without executing",
                            _next_round,
                        )
                        _on_repair_complete(0)
                else:
                    # 2026-09-17 — same treatment as
                    # the main loop's dead end: "原本就已经跳到了
                    # taskready，你直接去跳到那个 executing 状态…
                    # 如果发现没有任务，那不就跳转到 verification
                    # 吗？"
                    #
                    # An empty repair queue is not convergence, it is
                    # "go drain whatever execution work remains". The
                    # round already left the plan in
                    # ``verification_repairing``, so this is the
                    # declared ``verification_repairing → executing``
                    # edge. The executor decides whether there is
                    # work; finding none just exits and hands the
                    # chain back here.
                    #
                    # The round budget still bounds this: the guard
                    # at the top of this callback stops the recursion
                    # once ``_next_round`` would exceed the cap.
                    _dead_end_terminal(
                        plan_id,
                        _result.get("stop_reason")
                        or "no_repair_tasks",
                        chain_ending=False,
                        rollback_to_ready=False,
                    )
                    _server.logger.warning(
                        "[Auto-Verification] Post-repair round %d: no "
                        "repair tasks — routing to executing to drain "
                        "remaining work plan=%s",
                        _next_round, plan_id,
                    )
                    # 2026-10-22: the operator stopped the plan — do
                    # not route it to ``executing`` and do not dispatch
                    # the executor. The check sits BEFORE the routing
                    # call so the plan is not moved into a phase that
                    # says "work is about to run".
                    import verification_cancel as _vc_dispatch
                    if _vc_dispatch.is_cancelled(plan_id):
                        _server.logger.warning(
                            "[Auto-Verification] stop requested — not "
                            "dispatching repair plan=%s", plan_id,
                        )
                        return
                    try:
                        _new_orch.confirm_repair_and_rerun()
                    except Exception:  # noqa: BLE001
                        _server.logger.exception(
                            "[Auto-Verification] could not route the "
                            "post-repair dead end to executing "
                            "plan=%s — stopping", plan_id,
                        )
                        _record_terminal(
                            "failed", "post_repair_reroute_failed",
                        )
                    else:
                        _server._run_repair_execution_async(
                            plan_id, project_dir, tool=tool,
                            on_complete=_on_repair_complete,
                        )
            except Exception:
                _server.logger.exception(
                    "[repair_async] post-repair verification "
                    "crashed plan=%s",
                    plan_id,
                )
                _record_terminal(
                    "failed", "post_repair_verification_crashed",
                )
        except Exception:
            _server.logger.exception(
                "[repair_async] _on_repair_complete crashed "
                "plan=%s",
                plan_id,
            )
            try:
                _record_terminal(
                    "failed", "repair_callback_crashed",
                )
            except Exception:
                pass
    return _on_repair_complete


def _run_auto_verification_loop_inner(plan_id: str, plan_dir: Path, project_dir: Path, max_rounds: int = _server.DEFAULT_MAX_VERIFICATION_ROUNDS, tool: Optional[str] = None, start_round: int = 1, outcome: Optional[_VerificationLoopOutcome] = None):
    """Run verification loop automatically after execution completes.

    Handles the full verification-repair-retry cycle without user intervention:
    1. Run verification agent
    2. If passed -> transition to completed
    3. If failed -> generate repair tasks, re-execute, re-verify (up to max_rounds)
    4. If max rounds reached or same failure repeated -> transition to failed

    Mirrors the manual ``POST /api/verification/{plan_id}/start`` path by
    registering the run in ``_verification_state`` so that
    ``GET /api/verification/{plan_id}/status`` and ``HeartbeatMonitor`` can
    observe it and prevent duplicate auto-starts.

    ``start_round`` lets callers (typically the manual ``/start`` endpoint)
    enter the loop at a round other than 1 — e.g. ``start_round=2`` will
    skip Round 1's cycle and go straight to Round 2. Defaults to 1 (the
    post-execution auto-trigger case). All other loop invariants
    (``init_round`` idempotency, ``same_failure_repeated`` tracking,
    ``_persist_verification_terminal`` delegation) are unchanged.

    ``outcome`` is the wrapper's hand-off record (2026-09-23). The two
    repair-dispatch exits below set ``handed_off`` on it; every other
    exit leaves it alone, which is what makes "returned" and "finished"
    distinguishable. Direct calls may omit it — the loop then behaves
    exactly as before this parameter existed.
    """
    from datetime import datetime

    if outcome is None:
        outcome = _VerificationLoopOutcome()

    print(f"[Auto-Verification] Starting verification loop for {plan_id} (max_rounds={max_rounds}, tool={tool})")

    # Register in-memory state so the status API and HeartbeatMonitor can
    # observe the run. Use the same global lock as the manual start endpoint.
    with _server._verification_lock:
        existing = _server._verification_state.get(plan_id)
        if existing and existing.get("verification_status") == "running":
            print(f"[Auto-Verification] Verification already running for {plan_id}, skipping duplicate auto-start")
            return
        _init_verification_state(plan_id, max_rounds)

    # Materialise the plan state BEFORE the routing CAS below.
    #
    # The CAS operates on the ``plan_routing`` row, and that row comes
    # into existence through the PlanState write path (the INSERT arm
    # of ``RoutingRepository.write_plan_state``).  A plan that only
    # has a legacy ``plan_state.json`` — an imported plan — has no
    # routing row until a PlanState writes one, and the CAS would then
    # find nothing to move (``PlanNotFoundError``) and the phase
    # history would go unmaintained.
    #
    # The history itself is NOT maintained here: the CAS below owns it
    # and writes ``completed_phases`` in the SAME statement that moves
    # the phase.  This call only makes the row exist.
    _server.PlanState(plan_dir)

    # 2026-09-11 plan v9 (Bug 2 fix): CAS ``plan_routing.current_phase``
    # from ``executing`` (or ``queued`` for the partial-completion
    # edge case) into ``verification``. Without this CAS the routing
    # layer keeps reporting ``current_phase=executing`` even after the
    # verification round has begun, confusing downstream consumers
    # (the status endpoint forces ``status=running`` whenever
    # ``route.current_phase == "verification_running"`` or
    # ``route.current_phase == "executing"``). The CAS is best-effort:
    # ``ConflictError`` means another writer already advanced the
    # routing row, which is fine — we keep going.
    #
    # 2026-10-25 (R10): the source set follows the phase rename —
    # ``queued`` replaces ``ready``.  A plan rolled back by
    # ``_rollback_dead_end_to_queued`` now sits on ``queued``, and the
    # verification start has to accept it or the plan would be unable
    # to enter verification and would strand there.
    try:
        from state_machine.db.connection import open as _open_db_routing
        from state_machine.db.schema import migrate as _migrate_routing
        from state_machine.repositories.routing_repository import (
            ConflictError as _RoutingConflictError,
            RoutingRepository,
        )
        _rc = _open_db_routing(_server._state_db_path())
        try:
            _migrate_routing(_rc)
            RoutingRepository(_rc).try_mark_phase(
                plan_id,
                ("executing", "queued"),
                "verification",
            )
        except _RoutingConflictError:
            # Another writer already advanced — fine. The
            # verification loop doesn't depend on this CAS
            # succeeding; ``plan_state`` (transition_to) is the
            # authoritative layer.
            pass
        finally:
            _rc.close()
    except Exception:
        # Routing layer hiccups must NOT block the verification
        # loop. Log to stderr so operators can spot persistent
        # routing-layer issues.
        import sys as _sys_routing
        print(
            f"[_run_auto_verification_loop] routing CAS failed for "
            f"plan={plan_id!r} (continuing without CAS)",
            file=_sys_routing.stderr,
        )

    ps = _server.PlanState(plan_dir)
    ps.set_verification_max_rounds(max_rounds)
    previous_failed_ids = None

    # Register the current thread so HeartbeatMonitor can detect silent deaths.
    current_thread = threading.current_thread()
    v_state = _server._verification_state.get(plan_id)
    if v_state:
        v_state["thread"] = current_thread

    def _record_terminal(status: str, stop_reason: str):
        """Update the in-memory _verification_state, then call the
        top-level ``_persist_verification_terminal`` helper to
        advance the plan-level state machine (SQLite
        ``plan_verification`` + ``plan_routing``).

        2026-08-25 audit: the previous implementation only updated
        the in-memory ``_verification_state`` dict. The
        ``plan_verification.verification_status`` SQLite column
        stayed at "running" indefinitely, AND the
        ``plan_routing.current_phase`` column stayed at
        "verification_running" — and the public /status endpoint
        (``_verification_status_from_db``) hard-forces the API
        response to "running" whenever ``route.stage ==
        "verification_running"`` (server.py:4984-4985). So a
        plan whose auto-loop exited could *never* be observed
        as terminal via /status until routing was also advanced
        out. A round can run every VP and write its report, while
        ``verification_status`` and ``route.stage`` never
        advance out of "running" until both rows are fixed by hand. This helper closes that gap by delegating
        to ``_persist_verification_terminal`` (a top-level helper
        kept testable in isolation).

        2026-09-11 plan v14: thin wrapper around the module-level
        ``_record_verification_terminal`` so unit tests can patch
        / mock it without re-implementing closure logic.
        """
        _server._record_verification_terminal(plan_id, status, stop_reason)

    def _record_round_verdict(
        status: str, stop_reason: Optional[str], result: Optional[dict],
    ) -> None:
        """Write the round's verdict to ``plan_verification`` and nothing else.

        Distinct from :func:`_record_terminal`, which additionally advances
        the plan-level state machine and fires ``plan_closed``. This is the
        half a REPAIR round needs: the round is over and has a verdict, but
        the plan is not finished — an executor is about to run the repairs
        and another round will follow. Firing the terminal path there would
        publish ``plan_closed`` and evict the notifier's in-memory plan
        state for a plan that is still very much running.

        What it fixes: the repair exits returned to the executor without
        writing any terminal status, so
        ``plan_verification.verification_status`` kept the ``running``
        value stamped at round start. ``/api/plan/{id}/status`` sources its
        top-level ``verification_status`` from that column, and the card
        header is a pure function of the snapshot — so the header reads
        "🔄 验证中" above a body correctly naming the running repair task,
        for the entire round. A repair round is long, so the card spends
        the whole of it contradicting its own body.

        Best-effort, like every other state write here: a failed write
        must not abort a repair round that is otherwise ready to run. The
        read-side reconciliation in ``routes.plans._build_plan_status``
        covers the case where this one does not land, and
        ``plan_status.find_divergences`` reports it when it happens.
        """
        if status not in {"passed", "failed", "loop_stopped"}:
            # A non-verdict ("running" during an in-place update, or an
            # unexpected spelling) is not something to persist as a round
            # outcome. Leave the row alone rather than writing a value
            # nothing else in the codebase knows how to read.
            return
        try:
            from state_machine.db.connection import open as _open_db
            from state_machine.db.schema import migrate as _migrate
            from state_machine.repositories.verification_repository import (
                VerificationRepository as _VerifRepo,
            )

            conn = _open_db(_server._state_db_path())
            try:
                _migrate(conn)
                _VerifRepo(conn).complete_round(
                    plan_id,
                    {
                        "status": status,
                        "stop_reason": stop_reason,
                        "recorded_by": "_record_round_verdict",
                        "results": (result or {}).get("results"),
                    },
                    status=status,
                    stop_reason=stop_reason,
                    # The round is over; the plan is not. Announcing a
                    # terminal here would tell every subscriber the plan
                    # is closed seconds before an executor starts on it.
                    publish_closed=False,
                )
            finally:
                conn.close()
            _server.logger.info(
                "[verification_terminal] round verdict recorded plan=%s "
                "status=%s stop_reason=%s (repair path)",
                plan_id, status, stop_reason,
            )
        except Exception:
            _server.logger.exception(
                "[verification_terminal] round verdict write failed plan=%s "
                "status=%s", plan_id, status,
            )

    # Create the orchestrator ONCE so its _previous_failed_ids state
    # persists across rounds — otherwise same_failure_repeated detection
    # (which closes the loop after two identical failed VPs) never fires
    # because the state is reset every iteration.
    #
    # 2026-10-25 (VP-031): 对账在**建 orchestrator 之前**跑一次 ——
    # orchestrator 在构造时读失败历史，所以它读到的必须是已经对过账的
    # 视图：判据层判掉的记录（没有定义的验证点、被取消的尝试）不该再
    # 进它的失败集合。
    _reconcile_failure_history(plan_id)
    try:
        coding_tool = _server.create_coding_tool(
            tool, cwd=str(project_dir), scene="verification",
            logger=_verification_event_logger(plan_dir),
        )
        # Wire the SQLite VerificationRepository so the agent can
        # persist Phase 1 execution envelopes to
        # ``plan_verification.execution_results`` (task #3.5).
        verif_repo = None
        try:
            from state_machine.repositories.verification_repository import (
                VerificationRepository,
            )
            _, _, verif_repo = _server._open_verification_state()
        except Exception:  # noqa: BLE001
            verif_repo = None
        orch = _server.VerificationOrchestrator(
            plan_dir, project_dir, coding_tool=coding_tool,
            verif_repo=verif_repo,
        )
        v_state = _server._verification_state.get(plan_id)
        if v_state:
            v_state["orchestrator"] = orch
    except Exception as e:
        print(f"[Auto-Verification] Failed to create orchestrator: {e}")
        _record_terminal("failed", f"orchestrator_init_failed: {e}")
        return

    for round_num in range(start_round, max_rounds + 1):
        print(f"[Auto-Verification] Round {round_num}/{max_rounds}")
        # 2026-09-15: never start another round once the
        # operator has stopped the plan — the stop may land during a
        # repair execution, between rounds.
        try:
            import verification_cancel as _vc_mod
            if _vc_mod.is_cancelled(plan_id):
                _server.logger.warning(
                    "[Auto-Verification] stop requested before round %d "
                    "plan=%s — exiting the auto-loop", round_num, plan_id,
                )
                return
        except Exception:  # noqa: BLE001
            pass
        v_state = _server._verification_state.get(plan_id)
        if v_state:
            # 2026-09-15: re-assert ``running`` (and the round number /
            # fresh timestamp) at round start — the previous round's
            # terminal status otherwise lingered for the whole of round
            # N ≥ 2, so /api/system/active reported zero verifications
            # and the card showed the old verdict. See
            # ``_mark_verification_round_running``.
            _mark_verification_round_running(plan_id, round_num)
            # 2026-09-14: the previous round's stop_reason / split
            # records are RESIDUAL the moment a new round begins — a
            # "verification_log_stale" left over from round N-1 made
            # /status claim round N was failed while it was healthy
            # (observed on a production plan: the status API surfaced the
            # round-2 watchdog stamp all through round 3). Clear them
            # at round start, like the recovered-state scrub does.
            v_state.pop("vp_splits", None)
        # 2026-08-19 audit: the auto-loop never called
        # ``verification.init_round`` so the ``plan_verification.round``
        # column stayed at the ``/start`` value forever. The status
        # endpoint then reported ``round=1`` for round 2 regardless of
        # how many rounds actually ran. Persist the round to the
        # authoritative SQLite store before each cycle.
        try:
            from state_machine.repositories.verification_repository import (
                VerificationRepository as _VR,
            )
            _, _routing_cl, _verif_cl = _server._open_verification_state()
            try:
                _verif_cl.init_round(
                    plan_id,
                    round_n=round_num,
                    max_rounds=max_rounds,
                )
            finally:
                pass
        except Exception:
            pass
        try:
            # 2026-08-25: pass ``resume=True`` for round > 1 so the
            # executor reuses the verdict cache (already-PASSED
            # VPs are filtered out by ``BaseExecutor.run``'s
            # ``completed_set`` check) instead of re-running every
            # VP from scratch. Round 1 still starts with a clean
            # slate (resume=False) so a freshly-edited
            # ``verification_plan.json`` always gets re-evaluated
            # from scratch.
            #
            # Pre-2026-08-25 behaviour: every round passed
            # ``resume=False``, which triggered
            # ``_clear_verification_state_files`` and re-ran all
            # 28 VPs each round even though only 1 VP actually
            # failed. Round 2 burned ~5 minutes and ~600k tokens
            # re-running 20 already-passed VPs before the user
            # noticed.
            #
            # 2026-10-25: the dispatch now goes through the plan's
            # verification-round lease, so two rounds of the same plan
            # cannot overlap (see the lease section above).  The lease
            # is taken BEFORE the round's preflight — the preflight is
            # what starts the services and seeds the fixture, and two
            # rounds doing that at once is what produced the 2026-10-24
            # incident.
            report, _round_conflict = _dispatch_verification_round(
                plan_id, plan_dir, orch, round_num,
                resume=(round_num > 1),
            )
            if _round_conflict is not None:
                # 2026-10-25: another live round owns this plan.  The
                # lease refused us BEFORE the preflight, so no service
                # was started, no fixture was rewritten and no VP was
                # dispatched.  The run ends here, explicitly, and the
                # refusal is recorded in the plan's execution log — the
                # round that owns the plan keeps its verdict.
                _record_round_lease_refusal(
                    plan_id, plan_dir, _round_conflict,
                )
                return
            # 2026-10-25 (repair-r1-33): the round's services were not
            # usable, so the round dispatched nothing and measured
            # nothing.  There is no verdict to repair against, and the
            # judgment phase must not run: it would read the blocked
            # round as a failure and send the repair chain after a
            # product nobody ever looked at.  The refusal itself was
            # written down by the dispatch.
            _preflight_blocked = _service_preflight_blocked_report(report)
            if _preflight_blocked is not None:
                _record_terminal(
                    "loop_stopped", SERVICE_PREFLIGHT_ERROR_KIND,
                )
                return
            # 2026-10-29: the round was blocked by the base-drift check
            # — the branch had drifted away from its base and merging
            # the base back left conflicts.  The round dispatched
            # nothing, so its report is empty: sending it to
            # ``check_cycle_conditions`` would read the block as a
            # failure and send the repair chain after a product nobody
            # ever looked at.
            _base_drift_blocked = _base_drift_blocked_report(report)
            if _base_drift_blocked is not None:
                if _base_drift_round_action(report) == "repair":
                    # The conflict went through the repair channel that
                    # already exists, so the round hands the plan to the
                    # executor the way every other repair round does.
                    # The callback is built HERE: the loop's own
                    # ``_on_repair_complete`` is bound further down,
                    # and this branch runs first.
                    #
                    # The work is a BASE MERGE, not a product-code fix,
                    # so the executor runs under the merge's own
                    # provider-routing scene.  The scene reaches the
                    # child process through the environment.
                    _base_drift_on_complete = _make_repair_on_complete(
                        plan_id, project_dir, tool,
                    )
                    _server._run_repair_execution_async(
                        plan_id, project_dir, tool=tool,
                        on_complete=_base_drift_on_complete,
                        outcome=outcome,
                        scene=base_drift.BASE_DRIFT_MERGE_SCENE,
                    )
                    return
                # No repair task behind the conflict: there is nothing
                # to repair with, so the round ends here and says why.
                _server.logger.warning(
                    "[Auto-Verification] Round %d blocked by base drift "
                    "— no repair task to run; stopping plan=%s",
                    round_num, plan_id,
                )
                _record_terminal(
                    "loop_stopped", BASE_DRIFT_CONFLICTS_STOP_REASON,
                )
                return
            # The round has run against the addresses the ledger names —
            # write them down (2026-10-25, repair-r1-25).
            _record_dispatch_bindings(
                plan_dir, plan_id,
                reason=(
                    f"round {round_num} dispatched to the addresses its "
                    f"services were bound to"
                ),
            )

            # 2026-09-15: the operator stopped this plan
            # while the round was running. The stop handler already killed
            # the in-flight sub-agents and parked the routing row; do NOT
            # run the judgment / repair chain — that used to re-stamp the
            # stage the operator had just parked and re-block ``/start``.
            try:
                import verification_cancel
                _stopped = verification_cancel.is_cancelled(plan_id)
            except Exception:  # noqa: BLE001
                _stopped = False
            if _stopped:
                _server.logger.warning(
                    "[Auto-Verification] Round %d aborted by operator "
                    "stop plan=%s — skipping judgment and repair",
                    round_num, plan_id,
                )
                return

            # 2026-10-23: a question the round asked a human is a
            # question the round WAITS for.  The judgment below must not
            # run while a question is still open: the answer is what the
            # judgment is waiting for.  The wait beats every 60 seconds
            # so the watchdog can see that a round blocked on a human is
            # waiting and not dead.
            blocked = _wait_for_open_verification_decisions(
                plan_id, round_num,
            )
            if blocked is not None:
                # Nobody answered, and nobody may answer in a human's
                # place: the round stops here and the plan waits for the
                # answer.  The watchdog tick owns the pause checkpoint.
                _pause_verification_for_human(plan_id, blocked, round_num)
                return

            # 2026-10-25 (VP-031): 生成修复任务**之前**对一次账。
            #
            # 这一步把上一轮落下的失败记录按判据层重新判一遍：判为
            # criterion_defect / infra 的记录被标上 ``superseded_by``，
            # 原记录留在文件里（审计不丢），但不再进入失败集合 —— 同一个
            # id 的旧记录不得每轮重新出现。幂等，跑几遍结论一样。
            _reconcile_failure_history(plan_id)

            result = orch.check_cycle_conditions(report, round_number=round_num)

            status = result.get("status", "failed")

            # 2026-09-12: when the orchestrator
            # rebuilds the report at end-of-round, ``snapshot_round_results``
            # may have already moved the previous round's results into
            # ``rounds[-1].results``. ``report.get("verification_results")``
            # can therefore be empty even when ``result["repair_tasks"]``
            # is non-empty (or vice versa). Count failed/passed across
            # BOTH the top-level field AND the last round snapshot so
            # the v_state summary text reflects what actually happened.
            results_lists = []
            top_level_results = report.get("verification_results", [])
            if top_level_results:
                results_lists.append(top_level_results)
            rounds_field = report.get("rounds") or []
            if (
                not top_level_results
                and isinstance(rounds_field, list)
                and rounds_field
            ):
                last_round = rounds_field[-1] if isinstance(rounds_field[-1], dict) else {}
                last_results = last_round.get("results") or []
                if isinstance(last_results, list) and last_results:
                    results_lists.append(last_results)
            failed: List[Dict[str, Any]] = []
            passed: List[Dict[str, Any]] = []
            for results_list in results_lists:
                for r in results_list:
                    if not isinstance(r, dict):
                        continue
                    if r.get("status") == "FAILED":
                        failed.append(r)
                    elif r.get("status") == "PASSED":
                        passed.append(r)
            v_state = _server._verification_state.get(plan_id)
            if v_state:
                v_state["verification_status"] = status
                v_state["verification_round"] = round_num
                v_state["repair_tasks"] = result.get("repair_tasks", [])
                v_state["stop_reason"] = result.get("stop_reason")
                v_state["updated_at"] = datetime.now().isoformat()
                v_state["results"] = {
                    "pytest_summary": f"{len(passed)} passed, {len(failed)} failed",
                    "llm_findings": report.get("summary", ""),
                    "performance_metrics": {},
                }
                v_state["execution_profile"] = report.get("execution_profile", {})
                # 2026-10-23: the intent gate's verdict belongs on the
                # state as well — a conflict that only lives in the
                # round's result is gone once the round ends.
                _intent_fields = _repair_intent_round_fields(result)
                v_state["intent_conflicts"] = (
                    _intent_fields["intent_conflicts"]
                )
                v_state["needs_operator_decision"] = (
                    _intent_fields["summary"]["needs_operator_decision"]
                )

            # 2026-09-11 plan v12 (cards.py repair_tasks 持久化):
            # The orchestrator's ``result["repair_tasks"]`` only lives
            # in ``_verification_state[plan_id]`` (in-memory). When the
            # server restarts, the in-memory dict is lost and the Feishu
            # card loses all repair-task visibility. Persist to
            # ``plans/{id}/verification_repair_tasks.json`` immediately
            # so the card builder can read it via the new
            # ``_build_verification_progress`` repair_tasks field,
            # regardless of server uptime.
            #
            # 2026-09-12 (RP-* persistence bug fix):
            # ROUND-WISE ACCUMULATION (schema v2). Previously the file
            # was overwritten each round — when round N had zero
            # repair_tasks (e.g. all VPs passed), round N-1's RP-*
            # rows vanished from disk and Feishu cards lost track of
            # in-flight repair work. The new schema keeps a list of
            # rounds and the readers aggregate across all rounds,
            # deduping by task_id and preferring the latest copy.
            # Legacy v1 single-round files (plan_id/round/tasks) are
            # auto-migrated to v2 on first write so old plans pick
            # up the new behaviour without an explicit migration.
            try:
                _rt_payload = result.get("repair_tasks", [])
                if not isinstance(_rt_payload, list):
                    _rt_payload = []
                _rt_path = _server._plan_dir(plan_id) / "verification_repair_tasks.json"
                _rt_path.parent.mkdir(parents=True, exist_ok=True)
                # Read existing file (legacy v1 or v2). Migrate v1 → v2
                # by wrapping the previous ``tasks`` list as a single
                # round entry.
                _existing_rounds: List[Dict[str, Any]] = []
                if _rt_path.exists():
                    try:
                        with _rt_path.open("r", encoding="utf-8") as _rtf_read:
                            _rt_existing = json.load(_rtf_read)
                        if isinstance(_rt_existing, dict):
                            # v2 schema — keep all prior rounds, but
                            # drop any round entry whose ``round`` is
                            # == ``round_num`` (we are about to write a
                            # fresh entry for the same round; previous
                            # copy is stale).
                            for entry in (_rt_existing.get("rounds") or []):
                                if not isinstance(entry, dict):
                                    continue
                                if entry.get("round") == round_num:
                                    continue
                                _existing_rounds.append(entry)
                            # v1 legacy: ``{round, tasks, generated_at}``
                            # at top level. Wrap as a single round entry.
                            if not _existing_rounds and (
                                _rt_existing.get("round") is not None
                                or _rt_existing.get("tasks") is not None
                            ):
                                _legacy_round = _rt_existing.get("round")
                                _legacy_tasks = _rt_existing.get("tasks")
                                if (
                                    _legacy_round is not None
                                    and isinstance(_legacy_tasks, list)
                                    and _legacy_round != round_num
                                ):
                                    # 2026-10-23: the legacy file is
                                    # itself a round snapshot — it may
                                    # already carry the intent facts, so
                                    # it is read through the same helper
                                    # instead of being rebuilt by hand.
                                    # Otherwise a conflict recorded by
                                    # the repair-resume path would be
                                    # dropped by the very migration that
                                    # is supposed to preserve it.
                                    _existing_rounds.append(
                                        _build_repair_round_entry(
                                            _legacy_round,
                                            _legacy_tasks,
                                            _rt_existing,
                                            generated_at=_rt_existing.get(
                                                "generated_at"
                                            ),
                                        )
                                    )
                    except Exception:
                        # Corrupt or unreadable existing file — start
                        # fresh rather than crashing the round write.
                        _existing_rounds = []
                _new_round_entry = _build_repair_round_entry(
                    round_num, _rt_payload, result,
                )
                _existing_rounds.append(_new_round_entry)
                with _rt_path.open("w", encoding="utf-8") as _rtf:
                    json.dump(
                        {
                            "plan_id": plan_id,
                            "schema_version": 2,
                            "rounds": _existing_rounds,
                        },
                        _rtf,
                        ensure_ascii=False,
                        indent=2,
                    )
            except Exception:
                # Persistence is best-effort; in-memory state still
                # carries the live copy. The card builder will fall
                # back to in-memory if the file is missing.
                _server.logger.exception(
                    "[verification_repair_tasks_persist] failed plan=%s",
                    plan_id,
                )

            if status == "passed":
                # 2026-09-12: even when the round PASSED, there
                # may be pending RP-* tasks from an earlier round that
                # the orchestrator's ``same_failure_repeated`` branch
                # (now bounded by ``_MAX_CONSECUTIVE_SAME_FAILURE_ROUNDS``)
                # orphaned. run pending RP-* tasks regardless of the
                # current round's outcome — only treat as PASSED when
                # both verification AND the executor queue are clear.
                _pending = _get_pending_repair_tasks(
                    plan_id, _round_failed_vp_ids(orch, report),
                )
                if _pending:
                    _server.logger.info(
                        "[Auto-Verification] Round %d PASSED but %d "
                        "pending RP-* tasks remain — running executor first",
                        round_num, len(_pending),
                    )
                    repair_tasks = _pending
                    # 2026-09-12 plan v3 (closed-loop fix): the
                    # orchestrator returned ``status="passed"`` because
                    # the round's verdicts were all PASSED, but the
                    # overall chain has not converged — pending RP-*
                    # tasks are still in the executor queue. The state
                    # machine vocabulary must reflect this: the plan
                    # is in a non-terminal repair state, NOT in
                    # ``verification_passed``. ``confirm_repair_and_rerun``
                    # calls ``start_repair_execution`` which requires
                    # ``current_phase == 'verification_repairing'``;
                    # ``verification_passed`` is a terminal stage with
                    # no forward edges.
                    #
                    # Override the status so the downstream
                    # ``verification_failed`` → ``start_verification_repair``
                    # path in :class:`VerificationOrchestrator` runs and
                    # walks the plan into ``verification_repairing``
                    # before :meth:`confirm_repair_and_rerun` is called.
                    # The same override applies to the
                    # ``same_failure_repeated`` branch below.
                    result = dict(result)
                    result["status"] = "failed"
                    result["stop_reason"] = "pending_repair_tasks"
                    status = "failed"
                else:
                    # No pending RP-* tasks — handle the PASSED case
                    # via the helper (transition + record terminal).
                    _handle_passed_round(plan_id, round_num, ps)
                    return

            # Failure path
            stop_reason = result.get("stop_reason")
            # 2026-09-20 (post-mortem): key this off the orchestrator's
            # own ``status``, NOT off a spelling of ``stop_reason``.
            #
            # ``check_cycle_conditions`` returns ``status="loop_stopped"``
            # from exactly one place — its same-failure convergence branch —
            # and it writes that field and ``stop_reason`` in the same
            # return literal, so the two cannot drift apart. The equality
            # test this replaces compared against ``same_failure_repeated``,
            # a spelling the orchestrator stopped emitting on 2026-09-12
            # when the consecutive-rounds counter renamed it to
            # ``same_failure_repeated_after_max_attempts``. The guard never
            # matched, so the verdict was ignored: the round fell through
            # to the empty-repair-queue exit, which routes to ``executing``
            # (an edge that does not exist from ``verification_loop_stopped``)
            # and blew up inside ``confirm_repair_and_rerun`` — the blanket
            # ``except`` then started another full round. Observed live on
            # that run: rounds 3 and 4 both logged the convergence verdict and
            # the loop ran round 4 anyway, ending on ``max_rounds_reached``.
            # See ``VERIFICATION_CONVERGENCE_STOP_REASONS``.
            #
            # This is also what the post-repair callback
            # (``_on_repair_complete``) already does. The two exits must
            # agree, or the same verdict terminates a plan on one path and
            # starts another round on the other.
            #
            # Close the round in ``plan_verification`` before branching.
            # Every TERMINAL exit below goes through
            # ``_record_terminal`` → ``_persist_verification_terminal`` →
            # ``VerificationRepository.complete_round``, which writes the
            # verdict to ``plan_verification``. The REPAIR exits return
            # straight to the executor without doing that, so the row
            # keeps the ``running`` value written at round start while
            # ``plan_routing`` has already recorded the terminal verdict.
            # Since ``/api/plan/{id}/status`` takes its top-level
            # ``verification_status`` from that row, the card header then
            # reads "🔄 验证中" above a body naming the running repair
            # task, for the whole round.
            #
            # Written here rather than at each exit so a future exit added
            # below cannot repeat the omission.
            _record_round_verdict(status, stop_reason, result)
            if status == "loop_stopped":
                # 2026-09-12: even when the loop has converged, there
                # may be pending RP-* tasks from earlier rounds that should
                # still be executed. pending RP-* tasks must run regardless
                # of what ``check_cycle_conditions`` returns.
                _pending = _get_pending_repair_tasks(
                    plan_id, _round_failed_vp_ids(orch, report),
                )
                if _pending:
                    _server.logger.warning(
                        "[Auto-Verification] Round %d: loop converged "
                        "(%s) but %d pending RP-* tasks — running them "
                        "before terminating",
                        round_num, stop_reason, len(_pending),
                    )
                    repair_tasks = _pending
                    # Skip the terminal branch — fall through to the
                    # executor-spawn path.
                    pass
                else:
                    print(
                        f"[Auto-Verification] Round {round_num}: "
                        f"converged ({stop_reason}) — stopping"
                    )
                    # 2026-09-07: removed ``ps.transition_to("failed")``
                    # here — when the orchestrator has already advanced
                    # the plan to ``verification_loop_stopped`` (see
                    # orchestrator.check_cycle_conditions), this transition
                    # raises ``Illegal transition`` and the blanket
                    # ``except Exception`` swallowed it, leaving the plan
                    # stranded. ``_record_terminal`` now delegates to
                    # ``_persist_verification_terminal`` step 3 which
                    # calls ``transition_to("failed")`` with a
                    # ``force_set_phase`` fallback for the terminal-re-entry
                    # case.
                    #
                    # ``loop_stopped`` (not ``failed``) is the honest status
                    # for this exit and matches the post-repair path at
                    # ``_on_repair_complete``; ``_persist_verification_terminal``
                    # maps either spelling of a convergence reason to
                    # ``loop_stopped`` anyway, so the two exits agree by
                    # construction.
                    _record_terminal("loop_stopped", stop_reason)
                    return
            if stop_reason == "max_rounds_reached":
                # 2026-08-25 audit: previous code logged "Per user
                # max_rounds is no longer a stopping
                # condition" and kept iterating. That decision was
                # never recorded in the docs and contradicts the
                # ``/start`` handler's ceiling check (server.py:5242)
                # which refuses to start a round above ``max_rounds``.
                # Honour the cap here: transition to failed with
                # stop_reason="max_rounds_reached" and return. The
                # card shows the user how many rounds ran; if they
                # want to keep going they have to call
                # ``/reset_rounds`` explicitly (which now refuses
                # to set ``new_max_rounds`` above the original cap).
                print(
                    f"[Auto-Verification] Round {round_num}: max_rounds_reached — "
                    f"stopping (cap {max_rounds} reached)"
                )
                # Same fix as same_failure_repeated above — the
                # plan is likely already in ``verification_loop_stopped``
                # at this point and the direct ``ps.transition_to("failed")``
                # raised ``Illegal transition``. Delegate to
                # ``_record_terminal``.
                _record_terminal("loop_stopped", "max_rounds_reached")
                return

            # Generate repair tasks and auto-confirm for re-execution.
            #
            # 2026-09-12: prefer pending RP-* tasks from
            # state.db over the orchestrator's
            # ``result["repair_tasks"]`` payload. The user's
            # if the executor queue has pending RP-*
            # tasks (from any prior round), the executor MUST run
            # them — the orchestrator's "empty repair_tasks" payload
            # (which happens when same_failure_repeated fires) does
            # NOT mean "no work to do"; it means "no new tasks to
            # generate". The actual executor work is whatever's still
            # pending in the DB.
            _pending_db = _get_pending_repair_tasks(
                plan_id, _round_failed_vp_ids(orch, report),
            )
            if _pending_db:
                _server.logger.info(
                    "[Auto-Verification] Round %d: %d pending RP-* "
                    "tasks in state.db — using them instead of "
                    "orchestrator's empty payload",
                    round_num, len(_pending_db),
                )
                repair_tasks = _pending_db
            else:
                repair_tasks = result.get("repair_tasks") or []
                if not isinstance(repair_tasks, list):
                    repair_tasks = []
            # 2026-09-14: a round whose only forward work
            # is a VP split must NOT run the execution phase — the split
            # children are *verified*, not implemented, so the executor
            # queue is genuinely empty. Computed BEFORE the dead-end
            # branch so an empty repair list caused by splitting is not
            # mistaken for "the chain converged" (which would terminalise
            # the plan with the children never verified).
            _pure_split = _round_is_pure_split(result)
            # 2026-09-11 plan v14 (repair→execution→verification
            # auto-chain): define the callback that chains the next
            # verification round after the executor subprocess exits.
            # The previous behaviour was synchronous
            # ``_run_repair_execution`` inside the auto-loop thread
            # followed by ``for round_num`` iteration — that path
            # silently dropped the post-repair re-verification on
            # ``max_rounds`` exhaustion. The new flow schedules a
            # fresh ``start_verification_cycle`` here, then chains
            # another repair if the cycle still fails, or records
            # terminal if it passes / loops out.
            # 2026-10-22: the callback is a module-level factory now
            # (``_make_repair_on_complete``) so the ``/start`` repair
            # resume path and the auto-loop run the SAME
            # implementation. The body is unchanged — only the binding
            # moved.
            _on_repair_complete = _make_repair_on_complete(
                plan_id, project_dir, tool,
            )

            if not repair_tasks and not _pure_split:
                # 2026-09-14 — check for a generation FAILURE before
                # treating the empty list as convergence. Both cases
                # arrive here with ``repair_tasks == []``; only one of
                # them means "nothing to repair".
                _gen_error = result.get("repair_generation_error")
                if _gen_error:
                    print(
                        f"[Auto-Verification] Round {round_num}: repair-task "
                        f"generation FAILED (not a convergence) — retrying in "
                        f"the next round instead of parking"
                    )
                    _gen_state = _server._verification_state.get(plan_id)
                    if _gen_state:
                        _gen_state["stop_reason"] = "repair_generation_failed"
                        _gen_state["repair_generation_error"] = _gen_error
                        _gen_state["updated_at"] = datetime.now().isoformat()
                    _repair_generation_failed(plan_id, _gen_error)
                    # 2026-09-17: "生成失败也是要重试的，
                    # 给一个重试的机会，跟任何的任务一样". A generation
                    # failure is a TRANSIENT fault (provider outage, hard
                    # timeout, an empty reply), so the loop must retry it
                    # like any other task rather than hand the plan back to
                    # the operator. ``continue`` advances ``round_num`` and
                    # re-runs the cycle, which re-attempts generation; the
                    # ``max_rounds`` budget bounds the retries.
                    #
                    # ``_repair_generation_failed`` still runs first so the
                    # failure is recorded on ``plan_verification``. It does
                    # NOT move the routing row — the loop is still working
                    # this plan, so the row stays inside the verification
                    # family (see its docstring).
                    continue

                # 2026-09-12: even when both the orchestrator
                # payload AND the DB queue are empty, we still need
                # to surface a meaningful stop_reason. The chain is meant
                # to keep running, but if there's nothing
                # left to run, the chain has truly converged.
                # Surface ``stop_reason`` from the orchestrator
                # result (e.g. ``same_failure_repeated_after_max_attempts``
                # after the 2026-09-12 fix) instead of the generic
                # ``no_repair_tasks`` so operators see why.
                _real_stop_reason = (
                    result.get("stop_reason") or "no_repair_tasks"
                )
                print(
                    f"[Auto-Verification] Round {round_num}: No "
                    f"repair tasks in orchestrator payload or DB "
                    f"({_real_stop_reason}) — re-entering verification"
                )
                # 2026-09-14 at this final exit the
                # dead end must disambiguate — roll back to ready
                # when execution work remains, else close the chain.
                # Previously `_record_terminal(..., "no_repair_tasks")`
                # stayed non-chain-ending (the reason is deliberately
                # absent from VERIFICATION_TERMINAL_STOP_REASONS because
                # it also fires as an interim event mid-flight), so the
                # routing stage was never advanced and the plan stranded
                # on the user-gated verification_repairing stage with an
                # empty repair list.
                _dead_end_terminal(
                    plan_id, _real_stop_reason,
                    chain_ending=False,
                    # Not parking, not chaining-off: the very next thing
                    # this round does is route to ``executing`` (below), so
                    # the row must stay inside the verification family —
                    # ``tasks_ready`` is a stage ``/execution/start`` CASes
                    # to ``executing`` with no verification-liveness guard.
                    rollback_to_ready=False,
                )
                # 2026-09-17: "原本就已经跳到了 taskready，
                # 你直接去跳到那个 executing 状态，它不就会去调度任务去
                # 执行吗？如果发现没有任务，那不就跳转到 verification 吗？"
                #
                # So an empty repair queue is not a convergence verdict — it
                # is the signal to go RUN whatever execution work remains.
                # ``check_cycle_conditions`` has already put the plan in
                # ``verification_repairing`` (``_safe_phase_call`` runs
                # before any repair content is generated), and
                # ``verification_repairing → executing`` is a declared edge,
                # so this is a normal transition rather than a forced state
                # write.
                #
                # Whether there is actually work is the EXECUTOR's question,
                # not ours — if it finds nothing it exits immediately and
                # ``_on_repair_complete`` hands the chain straight back to
                # verification. Keeping that decision in one place is what
                # makes the loop closed: executing always flows to
                # verification, and only the round budget or a repeated
                # failure set can end the chain.
                print(
                    f"[Auto-Verification] Round {round_num}: no repair tasks — "
                    f"routing to executing so the executor can drain any "
                    f"remaining work, then back to verification"
                )
                # 2026-10-22: the operator stopped the plan — do not
                # route it to ``executing`` and do not dispatch the
                # executor. The check sits BEFORE the routing call so
                # the plan is not moved into a phase that says "work is
                # about to run".
                import verification_cancel as _vc_dispatch
                if _vc_dispatch.is_cancelled(plan_id):
                    _server.logger.warning(
                        "[Auto-Verification] stop requested — not "
                        "dispatching repair plan=%s", plan_id,
                    )
                    return
                try:
                    orch.confirm_repair_and_rerun()
                except Exception as _reroute_exc:  # noqa: BLE001
                    # The transition needs the plan in ``verification_repairing``.
                    # If a watchdog stamp or an operator stop moved it, do not
                    # strand the round: fall back to another verification
                    # round, which re-derives the state from scratch.
                    _server.logger.exception(
                        "[Auto-Verification] could not route the dead end to "
                        "executing plan=%s — re-entering verification instead",
                        plan_id,
                    )
                    continue
                _server._run_repair_execution_async(
                    plan_id, project_dir, tool=tool,
                    on_complete=_on_repair_complete,
                    outcome=outcome,
                )
                return

            if _pending_db:
                print(
                    f"[Auto-Verification] Round {round_num}: Running "
                    f"{len(repair_tasks)} pending RP-* tasks from DB"
                )
            else:
                print(f"[Auto-Verification] Round {round_num}: Generated {len(repair_tasks)} repair tasks, re-executing...")

            # Auto-confirm repair and run re-execution via CLI
            #
            # 2026-10-22: the operator stopped the plan — do not route
            # it to ``executing`` and do not dispatch the executor. The
            # check sits BEFORE the routing call so the plan is not
            # moved into a phase that says "work is about to run".
            import verification_cancel as _vc_dispatch
            if _vc_dispatch.is_cancelled(plan_id):
                _server.logger.warning(
                    "[Auto-Verification] stop requested — not "
                    "dispatching repair plan=%s", plan_id,
                )
                return
            orch.confirm_repair_and_rerun()

            # 2026-09-08: single-writer refactor. The previous
            # code wrote ``verification_tasks_round_<N>.json`` and
            # passed it to ``_run_repair_execution`` which merged it
            # with ``tasks.json`` into ``tasks_with_repair_round_<N>.json``.
            # That second writer violated the
            # ``tasks.json`` read-only invariant post-plan-generation.
            # The orchestrator's :meth:`check_cycle_conditions` now
            # writes each repair task straight to ``state.db`` via
            # :meth:`PlanTaskRepository.add_task`; the executor's
            # ``_load_tasks`` Phase 2 reconciles them. No on-disk
            # snapshot is needed — spawn the executor subprocess
            # against canonical ``tasks.json`` only.
            #
            # 2026-09-11 plan v14: replace synchronous ``_run_repair_execution``
            # (which ``process.wait()``ed inside the auto-loop thread for
            # 5+ minutes, racing the verification watchdog) with
            # ``_run_repair_execution_async`` — spawn + return
            # immediately, chain the next verification round through the
            # ``on_complete`` callback that fires when the subprocess
            # exits. The closing ``return`` exits the auto-loop thread
            # (the chain resumes from the callback, not from
            # ``for round_num``).
            #
            # 2026-09-17 — ``_on_repair_complete`` used to be defined
            # HERE, just above this dispatch, with a comment claiming the
            # name "is only bound once execution reaches it". That was
            # true of its position, not of its dependencies: its closure
            # vars are ``plan_id`` / ``tool`` / ``project_dir`` /
            # ``_record_terminal``, all bound before the ``for`` loop. It
            # now lives above the dead-end branch so that branch can
            # dispatch the executor too (see the comment there).
            # 2026-09-14 — pure-split round: skip the
            # execution phase entirely and re-enter verification, which
            # now runs the split children. The callback is invoked with a
            # synthetic rc=0 because no subprocess ran; its body only
            # reloads state, bumps the round and launches the next
            # verification cycle. ``_on_repair_complete`` must be the
            # nested def above (the name is only bound once execution
            # reaches it — hence this dispatch living here, after it).
            if _pure_split:
                _split_ids = [
                    s.get("vp_id") for s in (result.get("vp_splits") or [])
                    if isinstance(s, dict)
                ]
                _server.logger.warning(
                    "[Auto-Verification] Round %d: pure split (%s) — "
                    "skipping the execution phase, re-entering verification",
                    round_num, ", ".join(str(i) for i in _split_ids),
                )
                _split_state = _server._verification_state.get(plan_id)
                if _split_state is not None:
                    _split_state["vp_splits"] = result.get("vp_splits") or []
                    _split_state["stop_reason"] = "vp_split_no_repair_tasks"
                    _split_state["updated_at"] = datetime.now().isoformat()
                try:
                    _on_repair_complete(0)
                except Exception:
                    _server.logger.exception(
                        "[Auto-Verification] pure-split chain crashed "
                        "plan=%s", plan_id,
                    )
                    _record_terminal("failed", "vp_split_chain_crashed")
                # 2026-09-23: this exit has no subprocess of its own to
                # report from — ``_on_repair_complete(0)`` ran inline, so
                # by the time it returns the nested chain has either
                # settled (nothing in flight, reap as usual) or dispatched
                # a repair of its own (still in flight, defer).
                outcome.handed_off = _server._plan_execution_in_flight(plan_id)
                return

            _server._run_repair_execution_async(
                plan_id, project_dir, tool=tool,
                on_complete=_on_repair_complete,
                outcome=outcome,
            )
            return
        except VerificationCancelled as cancelled:
            # 2026-10-22 — the operator stopped this plan while the
            # round was judging / generating repair tasks.
            #
            # This is NOT a failed round, so it must not be recorded as
            # one. The arm below would call ``_record_terminal
            # ("failed", …)``, which advances the plan to
            # ``verification_failed`` — a terminal verdict on a round
            # the operator paused on purpose, and the exact thing the
            # 2026-09-15 rework was about ("the verification thread
            # kept going … re-stamping the stage the operator had just
            # parked").
            #
            # So: no terminal record, no failure record, no repair
            # tasks, no retry. The phase is left exactly where the
            # stop left it — the stop route parks a verification-half
            # stop at ``verification`` ("the round is paused, not
            # finished"), and a stop during the repair executor at
            # ``verification_repairing``. Moving it from here would
            # fight that CAS, which this module is not allowed to do
            # (``routes/verification.py`` owns the stop's target
            # phase).
            #
            # What the operator gets from ``/start`` is the ordinary
            # path: the round is picked up again, and the repair
            # generation re-opens the session the stopped round was
            # using (see ``repair_generator.repair_content_session``),
            # so the interrupted work is not paid for twice.
            _server.logger.warning(
                "[Auto-Verification] Round %d cancelled by the operator "
                "stop plan=%s — no terminal record, no repair tasks",
                round_num, plan_id,
            )
            print(
                f"[Auto-Verification] Round {round_num}: cancelled by the "
                f"operator stop — {cancelled}"
            )
            return
        except Exception as e:
            print(f"[Auto-Verification] Round {round_num}: Exception - {e}")
            import traceback
            traceback.print_exc()
            # 2026-09-07: removed the inline ``ps.transition_to``
            # calls — when the orchestrator already advanced the plan
            # to ``verification_failed`` / ``verification_repairing``
            # before the exception, ``transition_to("verification_failed")``
            # raised ``Illegal transition`` and the
            # ``except ValueError as ve`` swallowed it without any
            # further state update. ``_record_terminal`` routes
            # through ``_persist_verification_terminal`` which
            # internally uses ``transition_to`` + ``force_set_phase``
            # for terminal re-entry, so the plan always settles.
            _record_terminal("failed", str(e))
            return

    # 2026-09-17 — REACHABLE, and no longer a "safety net".
    #
    # The stale comment here claimed the loop "only exits via explicit
    # return". That was true when every branch returned; the two
    # dead-end branches now ``continue`` so the plan re-enters
    # verification instead of parking, which means exhausting
    # ``range(start_round, max_rounds + 1)`` is a normal outcome.
    #
    # The reason is ``max_rounds_reached``, not ``exited_loop_unexpectedly``:
    # the plan did exactly what it was allowed to do and then ran out of
    # budget. Reporting it as an unexpected exit sent operators chasing a
    # control-flow bug that does not exist.
    _server.logger.warning(
        "[Auto-Verification] round budget exhausted plan=%s "
        "(start_round=%d max_rounds=%d) — stopping",
        plan_id, start_round, max_rounds,
    )
    _record_terminal("loop_stopped", "max_rounds_reached")

