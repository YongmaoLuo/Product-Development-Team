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

from typing import Callable, Dict, List, Optional, Tuple, Any
from pathlib import Path
from datetime import datetime
import json
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time

# Late binding into the application module: ``server`` owns the shared
# helpers, request models and module globals, and the suite monkeypatches
# them as ``server.<name>``. Reaching them through the module object —
# rather than importing them by value — is what keeps those patches
# effective. ``server`` seeds ``sys.modules['server']`` before importing
# this module (see the wiring at the bottom of server.py).
import server as _server

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

    # Static task ids from tasks.json.
    static_ids: set[str] = set()
    try:
        data = json.loads(tasks_file.read_text())
        tasks = data if isinstance(data, list) else data.get("tasks", [])
        for t in tasks:
            if isinstance(t, dict) and t.get("id"):
                static_ids.add(str(t["id"]))
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
    for tid in static_ids:
        entry = runtime.get(tid) if isinstance(runtime.get(tid), dict) else None
        if entry is None:
            # Never written to runtime overlay — the executor never
            # picked this task up.
            out["pending_or_unstarted"] += 1
            continue
        status = entry.get("status")
        if status in TERMINAL_SUCCESS:
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
                if dep_status in ("completed", "skipped", "superseded"):
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


def _recover_verification_states(plans_dir: Path) -> None:
    """Restore ``_verification_state[plan_id]`` from disk on startup.

    Only plans with ``verification_status == "running"`` are loaded
    (a terminal-state file is left on disk for audit but the
    in-memory entry is intentionally left empty so the API reflects
    the real state, not stale disk artifacts).
    """
    if not plans_dir.exists():
        return
    for plan_dir in plans_dir.iterdir():
        if not plan_dir.is_dir():
            continue
        data = _server._load_verification_runtime_state(plan_dir.name)
        if data is None:
            continue
        if data.get("verification_status") != "running":
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
        }
        _server.logger.info(
            "Recovered verification runtime state for plan %s (round=%s, status=%s)",
            plan_dir.name, data.get("verification_round"), data.get("verification_status"),
        )


def _reconcile_orphaned_verification_stages() -> None:
    """CAS routing rows stranded at ``verification_running`` /
    ``verification_rerunning`` whose ``plan_verification`` row is
    already terminal (2026-09-13 飞书卡片 split-brain fix).

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
    at ``verification_running`` / ``verification_rerunning`` whose
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
            "WHERE r.current_phase IN ('verification_running', "
            "'verification_rerunning')"
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


def _mark_failed_dead(plan_id: str) -> None:
    """Mark an execution as failed because its process is gone.

    Idempotent: only acts if status is still 'running'; only transitions
    plan_state when the current phase is not already terminal.
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

    try:
        persist_conn = open_db(_server._state_db_path())
        try:
            migrate(persist_conn)
            ExecutionRepository(persist_conn).update_status(plan_id, "failed")
        finally:
            persist_conn.close()
    except ExecPlanNotFoundError:
        pass

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
    """
    state = _server._verification_state.get(plan_id)
    if not state or state.get("verification_status") != "running":
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
            plan_dir = _server._plan_dir(plan_id)
            stale_latest: Optional[float] = None
            for glob_pattern, reason in (
                (
                    ("logs/verification_*.log", "logs/vp_attempts/*.log"),
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
            #     phase (report judgment + per-VP supplementary review)
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
            # 2026-08-26: second-chance liveness check — a long-running
            # sub-agent (e.g. pytest verification re-running the suite
            # inside one attempt) only calls ``mark_progress`` at stage
            # transitions, so ``last_progress_ts`` can age past the
            # threshold even while the sub-agent is actively streaming
            # stdout. Read ``scoped_tool._last_output_ts`` (updated on
            # every stdout line) and skip the kill if it's still fresh.
            # Without this, the watchdog killed healthy 12-minute pytest
            # runs at the 20-minute mark.
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
    """
    backend_dir = _server._BACKEND_DIR
    plan_dir = _server._plan_dir(plan_id)
    tasks_file = plan_dir / "tasks.json"

    log_path = plan_dir / "logs" / f"repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

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
        process, _log = _server._spawn_executor_subprocess(
            plan_id=plan_id,
            backend_dir=backend_dir,
            project_dir=project_dir,
            tasks_file=tasks_file,
            tool=tool,
            extra_args=None,
            log_path=log_path,
        )
        # Synchronous wait is safe here: the subprocess writes to a
        # log file, not a PIPE, so it never blocks waiting for a
        # reader. This mirrors how a supervisor handles long-running
        # agent subprocesses — see ``cmd.WaitDelay = 3 * time.Second`` for the Go reference.
        returncode = process.wait()
        return returncode == 0
    finally:
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


def _run_repair_execution_async(
    plan_id: str,
    project_dir: Path,
    tool: Optional[str] = None,
    on_complete: Optional[Callable[[int], None]] = None,
    outcome: "Optional[_VerificationLoopOutcome]" = None,
) -> dict:
    """Spawn executor subprocess for repair tasks; return immediately.

    Returns ``{"status": "started", "pid": <pid>}`` synchronously. The
    subprocess runs in background; ``on_complete`` fires once on
    subprocess exit (success or failure). On spawn failure,
    ``on_complete(-1)`` fires before this function returns.

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
    try:
        process, _log = _server._spawn_executor_subprocess(
            plan_id=plan_id,
            backend_dir=backend_dir,
            project_dir=project_dir,
            tasks_file=tasks_file,
            tool=tool,
            extra_args=None,
            log_path=log_path,
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
            _ExecRepo(_persist_conn).update_phase(
                plan_id,
                current_phase="executing",
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
            "[repair_async] update_phase to plan_execution failed plan=%s",
            plan_id,
        )

    def _run() -> None:
        try:
            # ``process.wait()`` is safe — stdout/stderr redirected to
            # log file (no PIPE-buffer deadlock). The verification
            # watchdog is unaware of this thread, but the executor
            # watchdog (``_lazy_check_execution``) IS — it sees the
            # updated ``_execution_state[plan_id].pid`` and will fire
            # ``_mark_failed_dead`` if the subprocess crashes.
            process.wait()
            with _server._execution_state_lock:
                # 2026-09-11 plan v11: set the watchdog-friendly
                # "finished cleanly" flag BEFORE any subsequent state
                # mutation, mirroring the main execution path's fix
                # for the same race window.
                state["executor_finished_cleanly"] = True
                state["executor_returncode"] = process.returncode
                if process.returncode == 0:
                    state["status"] = "completed"
                    state["stop_reason"] = None
                else:
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

    # Usage-registry attribution: bind plan_id for every LLM call the
    # repair async path makes (repair generation + verification round).
    threading.Thread(
        target=_server._run_in_plan_ctx, args=(plan_id, _run), daemon=True
    ).start()
    if outcome is not None:
        outcome.handed_off = _server._plan_execution_in_flight(plan_id)
    return {"status": "started", "pid": process.pid}


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
    a split-brain state. Leave it ``None`` to keep the reason-based
    default (see ``VERIFICATION_TERMINAL_STOP_REASONS``).

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
    one of {``verification_running``, ``verification_rerunning``,
    ``verification_repairing``}):

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
    #   - status == "passed" (all VPs passed)
    #   - stop_reason is in ``VERIFICATION_TERMINAL_STOP_REASONS``
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
    # Hoisted above step 2 (it used to be computed inside step 2's ``try``,
    # so an import failure there left it unbound for step 3).
    _chain_ending = (
        chain_ending
        if chain_ending is not None
        else (
            status == "passed"
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
                            "verification_rerunning",
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


def _rollback_dead_end_to_ready(
    plan_id: str,
    unfinished_count: int,
    stop_reason: str,
    log_tag: str = "dead_end",
) -> bool:
    """Roll a dead-ended verification plan back to ``ready``.

    2026-09-14 when verification terminates with
    ``no_repair_tasks`` the plan must NOT strand on the user-gated
    ``verification_repairing`` phase — that gate only makes sense when
    there are repair tasks to confirm.  If execution work remains
    (``unfinished_count > 0``) the routing phase is CAS'd back to
    ``ready`` (the resumable-execution state ``/start`` accepts,
    server.py start_execution contract), so the operator can resume
    execution with an explicit ``POST /api/execution/{plan_id}/start``.
    We deliberately do NOT auto-start the executor — Ready != Start.

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
                    "verification_rerunning",
                    "verification_repairing",
                ),
                "ready",
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

    # Mirror current_phase so /api/plans and the summary endpoint agree.
    try:
        from state_machine.db.connection import open as _open_db
        from state_machine.db.schema import migrate as _migrate
        from state_machine.repositories.execution_repository import (
            ExecutionRepository as _ExecRepo,
        )

        conn = _open_db(_server._state_db_path())
        try:
            _migrate(conn)
            _ExecRepo(conn).update_phase(
                plan_id, "ready", create_if_missing=True
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — phase mirror is best-effort
        _server.logger.exception(
            f"[{log_tag}] current_phase mirror failed plan=%s", plan_id,
        )

    _server.logger.warning(
        f"[{log_tag}] plan=%s verification stopped (%s) with %d "
        "unfinished task(s) — rolled back to ready; resume "
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
    ``ready`` rollback on that same branch. The rollback exists so
    an OPERATOR can resume a parked plan via ``POST
    /api/execution/{plan_id}/start``; but that endpoint's CAS accepts
    ``ready`` and has **no verification-liveness guard**, so
    leaving the row there while the auto-loop runs another verification
    round opens a window where an operator restart spawns an executor
    into a mid-flight verification. A plan that is still being worked on
    must keep its routing row inside the verification family.
    """
    final_status = "failed" if reason == "no_repair_tasks" else "loop_stopped"
    _server._record_verification_terminal(plan_id, final_status, reason)
    unfinished = _count_unfinished_execution_tasks(plan_id)
    if unfinished > 0:
        if rollback_to_ready:
            _rollback_dead_end_to_ready(plan_id, unfinished, reason)
        else:
            _server.logger.warning(
                "[dead_end] plan=%s has %d unfinished execution task(s); "
                "NOT rolling routing back to ready — the auto-loop "
                "re-enters verification, and ready is a phase "
                "/execution/start would CAS away underneath it",
                plan_id, unfinished,
            )
        return
    if not chain_ending:
        return
    _server._persist_verification_terminal(
        plan_id, final_status, reason, chain_ending=True,
    )


def _repair_generation_failed(plan_id: str, error: str) -> None:
    """Record a repair-task generation FAILURE without closing the chain.

    ``result["repair_generation_error"]`` means the generator broke
    while failures were pending — the LLM call raised (provider outage,
    hard timeout) or replied with nothing usable. The verification run
    therefore produced no verdict about *what to fix*, which is NOT the
    same as "there is nothing to fix".

    The chain must not be closed in that case. We record the failure on
    ``plan_verification`` for visibility — the reason is deliberately
    absent from ``VERIFICATION_TERMINAL_STOP_REASONS``, so the routing
    stage is not advanced by it.

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
    _server._record_verification_terminal(plan_id, "failed", reason)


def _get_pending_repair_tasks(plan_id: str) -> List[Dict[str, Any]]:
    """Return all RP-* tasks in ``pending`` status from state.db.

    2026-09-12: regardless of whether the latest
    ``check_cycle_conditions`` produced new repair_tasks, the executor
    MUST pick up any RP-* tasks that are already pending from previous
    rounds and run them. The auto-loop previously only looked at
    ``result["repair_tasks"]`` returned by the orchestrator, which is
    empty when the orchestrator hit ``same_failure_repeated`` — leaving
    pending RP-* tasks orphaned forever and breaking the iteration
    chain.

    This helper reads ``state.db.plan_tasks`` directly via
    :class:`PlanTaskRepository.load_all` and filters for
    ``task_id LIKE 'RP-%'`` and ``status == 'pending'``. Empty list on
    DB error so the caller can fall back to the orchestrator's
    ``repair_tasks`` payload.
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
                "test_command, failed_vp_id, round, status "
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
                }
        finally:
            conn.close()
    except Exception:
        return []
    pending: List[Dict[str, Any]] = []
    # 2026-09-12 plan v2: accept BOTH the legacy ``RP-<n>`` id format
    # (pre-v9) AND the post-v9 ``R<round>-<i>`` id format. The two
    # share the ``task_group`` prefix ``repair`` (and its
    # ``repair-round-<N>`` variants) so we match on ``task_group``
    # rather than the literal ``RP-`` prefix — which would otherwise
    # silently drop R6-1 / R6-2 from the executor queue.
    # See also [[ac-repair-task-persistence-2026-09-12]] for the
    # parallel ``_TERMINAL_REPAIR_TASK_GROUP_PREFIX`` guard in the
    # refiner at agent.py:5940.
    def _is_repair_task_id(tid: str, task_group: Optional[str]) -> bool:
        if not isinstance(tid, str) or not tid:
            return False
        if tid.startswith("RP-") or tid.startswith("R") and "-" in tid:
            # Cover both legacy ``RP-*`` and ``R<n>-<i>`` shapes.
            return True
        # Defensive fallback: even if id has an unusual prefix, the
        # orchestrator-stamped ``task_group`` is the authoritative
        # marker — ``repair*`` indicates a repair task.
        return bool(task_group) and task_group.startswith("repair")
    for tid, entry in all_tasks.items():
        if not isinstance(entry, dict):
            continue
        if not _is_repair_task_id(tid, entry.get("task_group")):
            continue
        if entry.get("status") != "pending":
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
    1. Advance routing phase from ``verification_running`` /
       ``verification_rerunning`` to ``completed`` so the status
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
                ("verification_running", "verification_rerunning"),
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
    """
    v_state = _server._verification_state.get(plan_id)
    if v_state:
        v_state["verification_status"] = status
        v_state["stop_reason"] = stop_reason
        v_state["updated_at"] = datetime.now().isoformat()
        v_state["ended_at"] = v_state["updated_at"]

    _server._persist_verification_terminal(plan_id, status, stop_reason or "")


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
    """

    __slots__ = ("handed_off",)

    def __init__(self) -> None:
        self.handed_off = False

    @property
    def terminal(self) -> bool:
        """True iff no background execution will resume this workflow."""
        return not self.handed_off


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
    """
    outcome = _VerificationLoopOutcome()
    try:
        _server._run_auto_verification_loop_inner(
            plan_id, plan_dir, project_dir,
            max_rounds=max_rounds, tool=tool, start_round=start_round,
            outcome=outcome,
        )
    finally:
        if outcome.terminal:
            _server._reap_managed_services(plan_id, plan_dir, "verification_loop_exit")
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

    # 2026-09-11 plan v9 (Bug 2 fix): CAS ``plan_routing.current_phase``
    # from ``executing`` (or ``ready`` for the partial-completion
    # edge case) into ``verification``. Without this CAS the routing
    # layer keeps reporting ``current_phase=executing`` even after the
    # verification round has begun, confusing downstream consumers
    # (the status endpoint forces ``status=running`` whenever
    # ``route.current_phase == "verification_running"`` or
    # ``route.current_phase == "executing"``). The CAS is best-effort:
    # ``ConflictError`` means another writer already advanced the
    # routing row, which is fine — we keep going.
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
                ("executing", "ready"),
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
    try:
        coding_tool = _server.create_coding_tool(tool, cwd=str(project_dir), scene="verification")
        max_parallel = _server._resolve_max_parallel(coding_tool)
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
            max_parallel=max_parallel, verif_repo=verif_repo,
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
            report = orch.start_verification_cycle(
                round_number=round_num,
                force=False,
                resume=(round_num > 1),
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
                                    _existing_rounds.append({
                                        "round": _legacy_round,
                                        "generated_at": _rt_existing.get(
                                            "generated_at"
                                        ),
                                        "tasks": _legacy_tasks,
                                    })
                    except Exception:
                        # Corrupt or unreadable existing file — start
                        # fresh rather than crashing the round write.
                        _existing_rounds = []
                _new_round_entry = {
                    "round": round_num,
                    "generated_at": datetime.now().isoformat(),
                    "tasks": _rt_payload,
                }
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
                _pending = _get_pending_repair_tasks(plan_id)
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
                _pending = _get_pending_repair_tasks(plan_id)
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
                # condition" and kept iterating. That directive was
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
            _pending_db = _get_pending_repair_tasks(plan_id)
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
                        _report = _new_orch.start_verification_cycle(
                            round_number=_next_round,
                            force=False,
                            resume=True,
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
                                        "round": _next_round,
                                        "generated_at": (
                                            datetime.now().isoformat()
                                        ),
                                        "tasks": _rt_payload,
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
                        _pending_in_db = _get_pending_repair_tasks(plan_id)
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
                # to surface a meaningful stop_reason. The user wants
                # the chain to keep running, but if there's nothing
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

