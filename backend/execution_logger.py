"""
Execution Logger
================

Structured, persistent logger for autonomous execution.
Writes JSON-lines to plans/{plan_id}/execution.log with automatic rotation.
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config_paths import resolve_plans_dir


# Level priority for filtering
_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


# ---------------------------------------------------------------------------
# Current-activity derivation
# ---------------------------------------------------------------------------
#
# The operator's question is never "which phase is the plan in" but "what
# is it doing RIGHT NOW". For most of a run the answer is a task, and a
# task has a row in ``plan_tasks`` — so the card could name it. But a
# meaningful slice of wall-clock time is spent on work that is NOT a
# task and has no row: the refiner rewriting the task list, the
# executor crossing a layer boundary, the tail of a run after the last
# task completes.
#
# The refiner is the dominant case. It holds the executor for minutes at
# a time while it rewrites the task list, and it writes no row of its
# own, so ``current_task`` is None for that whole window. Two things
# follow, and they compound:
#
#   * the card renders "executing" and then names nothing, which reads
#     to the operator as a stall;
#   * nothing refreshes it either. ``_TASK_LIFECYCLE_EVENTS`` below
#     deliberately whitelists only the five ``task_*`` events — the
#     refiner is an implementation detail as far as the bus is
#     concerned — and the notifier's watch fingerprints task rows, which
#     do not move while the refiner runs. So the card freezes on
#     whatever the previous task left behind.
#
# This table maps log events onto the small closed vocabulary the card
# renders. It lives here, next to the log, because the log is the only
# durable record of what the executor subprocess is doing — the
# executor's in-bus events never reach the notifier, which runs in the
# server process. Display strings deliberately do NOT live here: this
# layer reports facts, ``notifications/cards.py`` owns presentation.
_ACTIVITY_EVENT_KINDS: Dict[str, str] = {
    "refine_started": "refine",
    "refine_structure_applied": "refine_done",
    "refine_completed": "refine_done",
    "refine_no_change": "refine_done",
    "refine_load_tasks_failed": "refine_done",
    "refine_exception": "refine_done",
    "layer_started": "layer_boundary",
    "layer_completed": "layer_boundary",
    "task_started": "task",
    "task_completed": "task_done",
    "execution_completed": "idle",
    "execution_stopped": "idle",
    "execution_failed": "idle",
}

#: Events that end whatever the executor was doing and leave it with
#: nothing in flight. After one of these the card should say the run is
#: over rather than keep naming the last thing it saw.
_ACTIVITY_TERMINALS = frozenset({"idle"})

#: Events that hand control back to task dispatch, leaving nothing in
#: flight. The boundary itself is NOT one of them: crossing a layer is
#: work the executor is doing (it is choosing and locking the next
#: batch), so it keeps its own kind and its own label rather than
#: collapsing into "idle".
_ACTIVITY_HANDOFFS = frozenset({"task_done", "refine_done"})


# Event type registry — maps execution-log event names to their on-disk
# render prefix. Used by log readers (frontend log panel, debug
# `grep SUBAGENT_* execution.log` workflows) to attribute a log line
# to its source module. The bracket-prefix form is the contract the
# downstream grep pipeline expects: a regression that drops the
# brackets (e.g. "SUBAGENT_PROVIDER: ..." instead of
# "[SUBAGENT_PROVIDER] ...") would silently break the grep.
#
# Subagent provider-isolation events (decision 3) live here so the
# tmpfile path (base_url / auth_token) can be located by
# `grep SUBAGENT_SETTINGS_FILE execution.log` and the resolved
# provider base_url by `grep SUBAGENT_PROVIDER execution.log`.
LOG_EVENT_TYPES = {
    'task_started': '[TASK_STARTED]',
    'task_completed': '[TASK_COMPLETED]',
    'task_failed': '[TASK_FAILED]',
    'task_retry': '[TASK_RETRY]',
    'task_timeout': '[TASK_TIMEOUT]',
    'task_api_error': '[TASK_API_ERROR]',
    'execution_started': '[EXECUTION_STARTED]',
    'execution_completed': '[EXECUTION_COMPLETED]',
    'execution_stopped': '[EXECUTION_STOPPED]',
    'execution_failed': '[EXECUTION_FAILED]',
    'loop_detected': '[LOOP_DETECTED]',
    'refine_started': '[REFINE_STARTED]',
    'refine_completed': '[REFINE_COMPLETED]',
    'refine_failed': '[REFINE_FAILED]',
    'refine_rejected': '[REFINE_REJECTED]',
    'breakdown_started': '[BREAKDOWN_STARTED]',
    'breakdown_completed': '[BREAKDOWN_COMPLETED]',
    'breakdown_failed': '[BREAKDOWN_FAILED]',
    'coding_query_started': '[CODING_QUERY_STARTED]',
    'coding_query_completed': '[CODING_QUERY_COMPLETED]',
    'coding_query_failed': '[CODING_QUERY_FAILED]',
    'coding_query_timeout': '[CODING_QUERY_TIMEOUT]',
    'plan_started': '[PLAN_STARTED]',
    'plan_completed': '[PLAN_COMPLETED]',
    'subagent_settings_file_written': '[SUBAGENT_SETTINGS_FILE]',
    'subagent_provider_used': '[SUBAGENT_PROVIDER]',
    'prd_self_review': '[PRD_SELF_REVIEW]',
    'arch_self_review': '[ARCH_SELF_REVIEW]',
    'test_self_review': '[TEST_SELF_REVIEW]',
}


# Subset of LOG_EVENT_TYPES that map to task lifecycle transitions
# the operator wants to see on the Feishu card. Other events
# (refine_*, breakdown_*, subagent_provider_used, etc.) are
# implementation noise that the notifier should ignore. Listed
# here as a frozen set so the notifier can iterate it cheaply and
# a future maintainer can grep for "task lifecycle events" to
# find every consumer.
_TASK_LIFECYCLE_EVENTS = frozenset({
    'task_started',
    'task_completed',
    'task_failed',
    'task_timeout',
    'task_retry',
})


def register_event_type(event_name: str, render_prefix: str) -> bool:
    """Register a new event type + render prefix, preserving any existing entry.

    Returns True if the event was newly registered, False if an entry
    already existed (in which case the original prefix is kept — the
    "don't overwrite" boundary). This is the single mutation point
    plugins/callers should use; direct dict mutation is a layering
    violation.
    """
    if event_name in LOG_EVENT_TYPES:
        return False
    LOG_EVENT_TYPES[event_name] = render_prefix
    return True


#: Where a tail read starts. 500 log entries run a few hundred bytes
#: each, so this is comfortably more than one request's worth; the
#: widening loop below covers the case where they are not, and the only
#: cost of starting small is one extra seek on an unusually verbose log.
#: Starting at 1 MiB instead was measurably slower on a multi-megabyte
#: log for no benefit, because the split-and-parse of a big blob
#: dominates and the answer almost never needs it.
_TAIL_INITIAL_BYTES = 256 * 1024  # 256 KiB


def _read_tail(log_file: Path, limit: int) -> List[Dict[str, Any]]:
    """Parse at most the last ``limit`` JSON-lines entries of ``log_file``.

    Seeks backwards from EOF rather than reading the file whole, so the
    cost is the same whether the log is a kilobyte or a gigabyte.

    The two limits are not independent and must not be allowed to
    disagree. ``limit`` bounds how many entries come back; the byte cap
    bounds how much is read to find them. If entries are long enough
    that ``limit`` of them do not fit in the cap, a naive seek returns
    too few — or none — and the caller degrades to "unknown", which is
    the card going blank for a reason that has nothing to do with the
    executor. So the budget is widened until ``limit`` entries have been
    parsed or the whole file has been read, whichever comes first.

    Two edge cases are handled explicitly rather than left to luck:

    * **A partial first line.** Seeking lands mid-line, so the first
      fragment is discarded. It cannot be parsed anyway.
    * **Malformed and non-dict lines.** Skipped, and they do NOT count
      toward ``limit`` — otherwise a log with junk on every line would
      stop the reader early.
    """
    try:
        size = log_file.stat().st_size
    except OSError:
        return []
    if size == 0 or limit <= 0:
        return []

    budget = min(size, _TAIL_INITIAL_BYTES)
    while True:
        start = max(0, size - budget)
        try:
            with open(log_file, "rb") as f:
                if start:
                    f.seek(start)
                blob = f.read()
        except OSError:
            return []

        text = blob.decode("utf-8", errors="replace")
        lines = text.split("\n")
        if start:
            # The seek almost certainly landed mid-line; drop the
            # fragment. ``start`` is a line boundary only by accident.
            lines = lines[1:]

        # Walk backwards and stop as soon as ``limit`` entries are in
        # hand. Parsing forwards and slicing at the end would decode
        # every line in the budget to keep the last handful, which is
        # the cost the seek was introduced to avoid.
        entries: List[Dict[str, Any]] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
                if len(entries) >= limit:
                    break
        entries.reverse()

        if len(entries) >= limit or start == 0:
            return entries
        # Ran out of budget before filling ``limit`` and there is more
        # file to the left — widen and try again.
        budget = min(size, budget * 4)


class ExecutionLogger:
    """Thread-safe structured logger that persists execution events to disk.

    Each log entry is a JSON line:
        {"ts": "2026-05-19T10:30:00.123456", "level": "INFO", "event": "task_started",
         "message": "...", "task_id": "1-2", "phase": "task_execution", "data": {...}}

    Log files rotate at MAX_BYTES with BACKUP_COUNT backups.
    """

    MAX_BYTES = 10 * 1024 * 1024  # 10 MB
    BACKUP_COUNT = 3

    def __init__(self, plan_id: str, plans_dir: Optional[Path] = None):
        self.plan_id = plan_id
        if plans_dir is None:
            # 2026-09-13: ``PDT_PLANS_DIR``-aware, so a test that logs
            # under a fixture plan_id does not create
            # ``<repo>/plans/<fixture-id>/`` in the operator's live tree.
            plans_dir = resolve_plans_dir()
        self._log_dir = plans_dir / plan_id
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = self._log_dir / "execution.log"
        self._lock = threading.Lock()

    # -- Public convenience methods --

    def debug(self, event: str, message: str, **kwargs):
        self.log("DEBUG", event, message, **kwargs)

    def info(self, event: str, message: str, **kwargs):
        self.log("INFO", event, message, **kwargs)

    def warning(self, event: str, message: str, **kwargs):
        self.log("WARNING", event, message, **kwargs)

    def error(self, event: str, message: str, **kwargs):
        self.log("ERROR", event, message, **kwargs)

    def critical(self, event: str, message: str, **kwargs):
        self.log("CRITICAL", event, message, **kwargs)

    # -- Core log method --

    def log(
        self,
        level: str,
        event: str,
        message: str,
        task_id: Optional[str] = None,
        phase: Optional[str] = None,
        data: Optional[dict] = None,
    ):
        """Write a structured log entry."""
        level = level.upper()
        if level not in _LEVEL_ORDER:
            level = "INFO"

        entry = {
            "ts": datetime.utcnow().isoformat(),
            "level": level,
            "event": event,
            "message": message,
        }
        if task_id:
            entry["task_id"] = task_id
        if phase:
            entry["phase"] = phase
        if data:
            entry["data"] = data

        line = json.dumps(entry, ensure_ascii=False, default=str)

        with self._lock:
            self._rotate_if_needed()
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")

        # Also print for backward compat (stdout captured by server deque)
        prefix = f"[{entry['ts']}] [{level}]"
        if task_id:
            prefix += f" [{task_id}]"
        print(f"{prefix} {event}: {message}")

        # State-change hook: a subset of execution-log events map
        # to task lifecycle transitions the operator wants to see
        # on the card (started / completed / failed / timeout).
        # Other events (refine_*, breakdown_*, etc.) are
        # implementation noise that the notifier should ignore —
        # whitelisting keeps the queue from drowning in non-state
        # changes.
        if event in _TASK_LIFECYCLE_EVENTS:
            try:
                from notifications.state_events import (
                    KIND_TASK_STATE_CHANGED,
                    publish_safe,
                )
                publish_safe(
                    KIND_TASK_STATE_CHANGED,
                    self.plan_id,
                    sub_kind="execution_log",
                    event=event,
                    task_id=task_id,
                    status=event.split("_", 1)[-1] if event.startswith("task_") else None,
                )
            except Exception:
                # Best-effort — see verification_persistence hook.
                pass

    # -- Rotation --

    def _rotate_if_needed(self):
        """Rotate log file if it exceeds MAX_BYTES."""
        try:
            if not self._log_file.exists():
                return
            size = self._log_file.stat().st_size
            if size < self.MAX_BYTES:
                return
        except OSError:
            return

        # Rotate: .log -> .log.1, .log.1 -> .log.2, ..., drop .log.{BACKUP_COUNT}
        for i in range(self.BACKUP_COUNT, 0, -1):
            src = self._log_file.parent / f"execution.log.{i}"
            if i == self.BACKUP_COUNT:
                if src.exists():
                    src.unlink()
                continue
            dst = self._log_file.parent / f"execution.log.{i + 1}"
            if src.exists():
                src.rename(dst)

        # .log -> .log.1
        self._log_file.rename(self._log_file.parent / "execution.log.1")

    # -- Static helpers for reading logs (used by server API) --

    @staticmethod
    def read_logs(
        plan_id: str,
        plans_dir: Optional[Path] = None,
        level: Optional[str] = None,
        task_id: Optional[str] = None,
        event: Optional[str] = None,
        limit: int = 100,
        since: Optional[str] = None,
    ) -> list:
        """Read and filter log entries from disk.

        Returns list of parsed log entry dicts, newest first within limit.
        """
        if plans_dir is None:
            plans_dir = resolve_plans_dir()

        log_file = plans_dir / plan_id / "execution.log"
        if not log_file.exists():
            return []

        min_level = _LEVEL_ORDER.get(level.upper(), 0) if level else 0

        entries = []
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Apply filters
                    if min_level > 0:
                        entry_level = _LEVEL_ORDER.get(entry.get("level", "INFO"), 1)
                        if entry_level < min_level:
                            continue

                    if task_id and entry.get("task_id") != task_id:
                        continue

                    if event and entry.get("event") != event:
                        continue

                    if since:
                        if entry.get("ts", "") < since:
                            continue

                    entries.append(entry)
        except OSError:
            pass

        # Return newest entries within limit
        return entries[-limit:]

    @staticmethod
    def current_activity(
        plan_id: str, plans_dir: Optional[Path] = None
    ) -> Dict[str, Any]:
        """Return what the executor is doing right now, as a fact record.

        ``{"kind", "started_at", "task_id", "event", "detail"}`` where
        ``kind`` is one of the ``_ACTIVITY_EVENT_KINDS`` values,
        ``started_at`` is the ISO timestamp the unit of work began, and
        ``task_id`` / ``detail`` are populated when the log carried them.

        Returns ``{"kind": "unknown", ...}`` when the log is missing or
        carries no recognisable event — the card then falls back to the
        bare phase label rather than inventing an activity.

        This is a FALLBACK, not a replacement for ``current_task``: when
        a ``plan_tasks`` row is ``in_progress`` that row is authoritative
        (it carries the task title the executor actually committed to),
        and this is consulted only for the windows where no such row
        exists. Deriving it from the log rather than from in-memory
        executor state is deliberate — the executor is a separate
        process whose bus events never reach the notifier, but its log
        is on disk and survives a server restart, which is the same
        reason :meth:`diagnose` reads the file.
        """
        empty: Dict[str, Any] = {
            "kind": "unknown",
            "started_at": None,
            "task_id": None,
            "event": None,
            "detail": None,
        }
        if plans_dir is None:
            plans_dir = resolve_plans_dir()

        # Read the TAIL, not the file. ``read_logs`` parses every line and
        # then discards all but the last N — the right shape for
        # ``diagnose`` (it wants the whole history) and the wrong shape
        # here: this runs on ``/api/plan/{id}/status`` and
        # ``/api/execution/{id}/progress``, which the notifier polls
        # several times a minute per plan. A long run's log is megabytes
        # and grows without bound, so a full parse would put an
        # ever-growing cost on the endpoint that renders the card.
        #
        # 500 lines is far more than the tail this ever inspects: the
        # longest gap between activity events is a refine window, which
        # emits on the order of tens of lines. The byte cap inside
        # ``_read_tail`` keeps one pathological line from pulling in the
        # whole file.
        entries = _read_tail(
            plans_dir / plan_id / "execution.log", limit=500,
        )
        if not entries:
            return empty

        for entry in reversed(entries):
            event = entry.get("event")
            kind = _ACTIVITY_EVENT_KINDS.get(event or "")
            if kind is None:
                continue
            if kind in _ACTIVITY_TERMINALS or kind in _ACTIVITY_HANDOFFS:
                # The executor has nothing in flight right now. Report
                # the handoff moment itself so the card can say "just
                # finished X" rather than an unqualified "idle", and so
                # a genuinely wedged executor (nothing logged for an
                # hour) is distinguishable from one between tasks.
                return {
                    "kind": "idle",
                    "started_at": entry.get("ts"),
                    "task_id": entry.get("task_id"),
                    "event": event,
                    "detail": None,
                }
            data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
            return {
                "kind": kind,
                "started_at": entry.get("ts"),
                "task_id": entry.get("task_id"),
                "event": event,
                "detail": data.get("title") or None,
            }
        return empty

    @staticmethod
    def diagnose(plan_id: str, plans_dir: Optional[Path] = None) -> dict:
        """Generate an AI-friendly diagnostic summary from execution logs.

        Analyzes log entries to find patterns: repeated failures, timeouts,
        refinement loops, stuck durations, etc.
        """
        if plans_dir is None:
            plans_dir = resolve_plans_dir()

        # Read all error/warning/critical entries
        all_entries = ExecutionLogger.read_logs(
            plan_id, plans_dir, limit=10000
        )

        if not all_entries:
            return {
                "plan_id": plan_id,
                "status": "no_logs",
                "diagnosis": "No execution logs found for this plan.",
                "suggestions": [],
            }

        # Classify entries
        errors = [e for e in all_entries if e.get("level") in ("ERROR", "CRITICAL")]
        warnings = [e for e in all_entries if e.get("level") == "WARNING"]
        refines = [e for e in all_entries if e.get("event") == "refine_completed"]
        refine_fails = [e for e in all_entries if e.get("event") == "refine_failed"]
        task_failures = [e for e in all_entries if e.get("event") == "task_failed"]
        task_retries = [e for e in all_entries if e.get("event") == "task_retry"]
        task_timeouts = [e for e in all_entries if e.get("event") == "task_timeout"]
        loop_detections = [e for e in all_entries if e.get("event") == "loop_detected"]

        # Determine current task and status
        current_task = None
        last_ts = all_entries[-1].get("ts", "") if all_entries else ""
        first_ts = all_entries[0].get("ts", "") if all_entries else ""

        # Find last task that was started
        for e in reversed(all_entries):
            if e.get("event") == "task_started":
                current_task = {
                    "id": e.get("task_id"),
                    "title": e.get("data", {}).get("title", ""),
                    "attempt": e.get("data", {}).get("attempt", 1),
                    "started_at": e.get("ts"),
                }
                break

        # Detect stuck: last event is more than 10 min ago and no completion
        stuck_duration_sec = None
        status = "unknown"
        last_events = [e.get("event") for e in all_entries[-5:]]
        if "execution_completed" in last_events:
            status = "completed"
        elif "execution_stopped" in last_events:
            status = "stopped"
        elif "loop_detected" in last_events:
            status = "loop_detected"
        elif current_task:
            # Check if execution is still running
            last_event = all_entries[-1].get("event", "")
            if last_event in ("task_retry", "task_timeout", "task_started",
                              "refine_started", "refine_completed", "refine_failed",
                              "coding_query_started", "coding_query_completed"):
                status = "running_or_stuck"
                # Calculate stuck duration from last log entry
                try:
                    last_dt = datetime.fromisoformat(last_ts)
                    now_dt = datetime.utcnow()
                    stuck_duration_sec = int((now_dt - last_dt).total_seconds())
                except (ValueError, TypeError):
                    pass
            elif last_event in ("task_failed", "execution_failed"):
                status = "failed"

        # Group task failures by task_id
        task_failure_map = {}
        for e in task_failures:
            tid = e.get("task_id", "unknown")
            task_failure_map.setdefault(tid, []).append(e)

        # Group retries by task_id
        task_retry_map = {}
        for e in task_retries:
            tid = e.get("task_id", "unknown")
            task_retry_map.setdefault(tid, []).append(e)

        # Build retry summary for the most-retried task
        retry_summary = None
        if task_retry_map:
            worst_task = max(task_retry_map, key=lambda k: len(task_retry_map[k]))
            retry_summary = {
                "task_id": worst_task,
                "total_attempts": len(task_retry_map[worst_task]) + 1,  # +1 for initial attempt
                "errors": [e.get("message", "") for e in task_retry_map[worst_task][-5:]],
            }

        # Recent errors (last 10)
        recent_errors = [
            {
                "ts": e.get("ts"),
                "task_id": e.get("task_id"),
                "event": e.get("event"),
                "message": e.get("message"),
            }
            for e in errors[-10:]
        ]

        # Refinement history
        refinement_history = [
            {
                "ts": e.get("ts"),
                "task_id": e.get("task_id"),
                "event": e.get("event"),
                "data": e.get("data"),
            }
            for e in (refines + refine_fails)[-10:]
        ]

        # Build diagnosis text
        diagnosis_parts = []
        if loop_detections:
            ld = loop_detections[-1]
            diagnosis_parts.append(
                f"CIRCULAR LOOP DETECTED: {ld.get('message', '')}"
            )
        elif status == "failed":
            if task_failures:
                last_fail = task_failures[-1]
                diagnosis_parts.append(
                    f"Execution failed. Last task [{last_fail.get('task_id')}] failed: "
                    f"{last_fail.get('message', '')}"
                )
        elif status == "running_or_stuck" and stuck_duration_sec and stuck_duration_sec > 300:
            diagnosis_parts.append(
                f"Execution appears stuck. No log activity for {stuck_duration_sec}s. "
                f"Current task: {current_task['id'] if current_task else 'unknown'}"
            )
        elif retry_summary and retry_summary["total_attempts"] >= 3:
            diagnosis_parts.append(
                f"Task {retry_summary['task_id']} has been retried "
                f"{retry_summary['total_attempts']} times. "
                f"Last errors: {'; '.join(retry_summary['errors'][-3:])}"
            )
        elif task_timeouts:
            last_timeout = task_timeouts[-1]
            diagnosis_parts.append(
                f"Task timeout detected for [{last_timeout.get('task_id')}]: "
                f"{last_timeout.get('message', '')}"
            )

        if refines:
            diagnosis_parts.append(
                f"Refiner was called {len(refines)} time(s), "
                f"indicating the system is struggling to pass tests."
            )

        if not diagnosis_parts:
            diagnosis_parts.append("No critical issues detected in logs.")

        diagnosis = " ".join(diagnosis_parts)

        # Build suggestions
        suggestions = []
        if retry_summary and retry_summary["total_attempts"] >= 5:
            suggestions.append(
                f"Task {retry_summary['task_id']} exceeded max retries. "
                "Consider: (1) check test_command correctness, "
                "(2) manually fix test setup, "
                "(3) skip this task."
            )
        if stuck_duration_sec and stuck_duration_sec > 600:
            suggestions.append(
                "No activity for >10min. The coding tool subprocess may have hung. "
                "Consider stopping and restarting execution."
            )
        if len(refines) >= 3:
            suggestions.append(
                "Multiple refinement cycles detected. The task may need to be "
                "manually broken down or the test may have a fundamental issue."
            )
        if task_timeouts:
            suggestions.append(
                "Timeouts detected. Consider increasing the timeout or "
                "simplifying the task."
            )
        if loop_detections:
            suggestions.append(
                "Circular loop: the same task completed before but the problem recurred. "
                "Manual intervention is required — the issue cannot be auto-resolved."
            )

        return {
            "plan_id": plan_id,
            "status": status,
            "current_task": current_task,
            "stuck_duration_sec": stuck_duration_sec,
            "last_log_ts": last_ts,
            "first_log_ts": first_ts,
            "recent_errors": recent_errors,
            "refinement_history": refinement_history,
            "retry_summary": retry_summary,
            "total_errors": len(errors),
            "total_warnings": len(warnings),
            "total_refinements": len(refines),
            "diagnosis": diagnosis,
            "suggestions": suggestions,
        }


def get_logger(plan_id: Optional[str] = None) -> Optional[ExecutionLogger]:
    """Get or create a logger for the given plan_id.

    If plan_id is None, tries PDT_PLAN_ID environment variable.
    Returns None if no plan_id is available (backward compat).
    """
    pid = plan_id or os.environ.get("PDT_PLAN_ID")
    if not pid:
        return None
    return ExecutionLogger(pid)
