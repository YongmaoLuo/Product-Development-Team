"""Watchdog entry point + dead-loop detection (architecture decision point 6).

The watchdog is an **independent process**, decoupled from the daemon:
it never imports the dispatcher, holds no daemon state, and polls the
plan directory from the outside. If the watchdog itself dies, the daemon
is unaffected — and vice versa.

Trigger condition
-----------------

PRD decision point 6 makes "dead loop" machine-decidable::

    在 task 状态存储中无任何 task 状态推进（无 status / commit_sha 变更）
    的前提下，daemon 重复进入同一 plan_id 下同一 task_id 的执行循环，
    计 1 次；连续计数 ≥3 次即触发 watchdog。

Two independent signals therefore gate the counter:

1. **Error fingerprint** — a sha256 over
   ``(plan_id, task_id, normalised_error_text)``. The normalisation
   strips volatile substrings (timestamps, hex addresses, UUIDs,
   durations) so the *same* underlying failure produces the *same*
   fingerprint across retries. A different fingerprint means a
   different failure, which is progress of a sort — the counter resets.

2. **Progress token** — a sha256 over every task row's
   ``(id, status, commit_sha)`` triple. Any status advance or new
   commit changes the token; the daemon made progress, so the loop is
   not dead and the counter resets.

Only when both are unchanged does the counter advance. At
:data:`DEADLOOP_THRESHOLD` (3) consecutive observations,
:attr:`DetectionResult.triggered` flips to ``True`` and the
``on_trigger`` hook fires — that hook is the seam where the action
sequence (LLM 调研 → auto-fix → validate-through → 重启, task 13) is
attached.

This module deliberately implements *detection only*. It reads
``execution.log`` (JSON-lines, written by
:class:`execution_logger.ExecutionLogger`) and ``tasks.json``; it
writes nothing.

Module name note
----------------

``backend/watchdog.py`` shadows the PyPI ``watchdog`` filesystem
package when ``backend/`` is on ``sys.path``. That package is not a
dependency of this project (nothing in ``backend/pyproject.toml``
names it and it is absent from ``backend/uv.lock``), and the
architecture document pins this exact path, so the shadowing is
intentional and inert.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Mapping, Sequence

from framework.clock import utcnow_iso
from framework.ids import InvalidPlanIdError, validate_plan_id
from config_paths import resolve_state_db_path

__all__ = [
    "DEADLOOP_THRESHOLD",
    "DeadLoopDetector",
    "DetectionResult",
    "Watchdog",
    "compute_fingerprint",
    "compute_progress_token",
    "main",
    "normalise_error",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Consecutive identical observations required to declare a dead loop.
#: Pinned to 3 by PRD decision point 6 / architecture decision point 6.
DEADLOOP_THRESHOLD: Final[int] = 3

#: Name of the JSON-lines log the daemon writes inside ``plans/{plan_id}/``.
EXECUTION_LOG_FILENAME: Final[str] = "execution.log"

#: Name of the task store inside ``plans/{plan_id}/``.
TASKS_FILENAME: Final[str] = "tasks.json"

#: Default polling interval for the long-running (non ``--once``) mode.
DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 30.0

#: Log ``event`` values that count as a failed execution round. These
#: are the ``ExecutionLogger`` event names that mean "the daemon tried
#: to run a task and it did not work out". Progress events
#: (``task_completed``, ``task_started``, ...) are deliberately absent.
FAILURE_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "task_failed",
        "task_timeout",
        "task_api_error",
        "task_retry",
        "loop_detected",
        "coding_query_failed",
        "coding_query_timeout",
        "breakdown_failed",
        "refine_failed",
    }
)

#: Task-row fields that constitute "state progression". Anything else
#: (title, description, depends_on, ...) is structural and does not
#: mean the daemon advanced.
_PROGRESS_FIELDS: Final[tuple[str, ...]] = ("status", "commit_sha")


# Volatile-substring patterns, applied in order. Each collapses a class
# of value that changes between otherwise-identical failures, so the
# fingerprint stays stable across retries. Order matters: the ISO
# timestamp pattern must run before the bare-number pattern, otherwise
# the digits inside a timestamp get replaced piecemeal.
_VOLATILE_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # ISO-8601 timestamps, with or without fractional seconds / offset.
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
        ),
        "<TS>",
    ),
    # UUIDs (canonical 8-4-4-4-12 form).
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<UUID>",
    ),
    # Hex addresses / handles (``0x7fdeadbeef``).
    (re.compile(r"\b0[xX][0-9a-fA-F]+\b"), "<ADDR>"),
    # Git sha-ish blobs (7+ hex chars standing alone).
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<HEX>"),
    # Durations (``1800.42s``, ``12s``, ``350ms``).
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds|m|min|mins)\b"), "<DUR>"),
    # Any remaining bare number (line numbers, byte counts, PIDs).
    (re.compile(r"\b\d+\b"), "<N>"),
)

_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def normalise_error(error_text: str) -> str:
    """Collapse volatile substrings so retries of one failure compare equal.

    Timestamps, UUIDs, hex addresses, git shas, durations and bare
    numbers are replaced with fixed placeholders, and runs of
    whitespace are squeezed to a single space. The result is
    lower-cased so casing drift in a re-raised message does not split
    the fingerprint.
    """
    text = str(error_text)
    for pattern, placeholder in _VOLATILE_PATTERNS:
        text = pattern.sub(placeholder, text)
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def _sha256(*parts: str) -> str:
    """Hash ``parts`` with a NUL separator so concatenation is unambiguous."""
    digest = hashlib.sha256()
    digest.update("\x00".join(parts).encode("utf-8"))
    return digest.hexdigest()


def compute_fingerprint(plan_id: str, task_id: str, error_text: str) -> str:
    """Return the stable sha256 fingerprint of one failed execution round.

    The fingerprint is scoped to ``(plan_id, task_id)`` so two plans —
    or two tasks within one plan — failing the same way never share a
    counter.

    Raises
    ------
    InvalidPlanIdError
        If ``plan_id`` is unsafe (traversal, absolute path, ...).
    """
    safe_id = validate_plan_id(plan_id)
    return _sha256(safe_id, str(task_id), normalise_error(error_text))


def compute_progress_token(tasks: Iterable[Mapping[str, Any]]) -> str:
    """Return a digest over every row's ``(id, status, commit_sha)`` triple.

    Rows are sorted by id first, so the token is independent of the
    order in which ``tasks.json`` happens to list them. Fields outside
    :data:`_PROGRESS_FIELDS` are ignored: a re-worded title is not
    progress.
    """
    rows: list[str] = []
    for row in tasks:
        if not isinstance(row, Mapping):
            continue
        values = [str(row.get("id", ""))]
        values.extend(str(row.get(field_name, "")) for field_name in _PROGRESS_FIELDS)
        rows.append("\x1f".join(values))
    rows.sort()
    return _sha256(*rows)


# ---------------------------------------------------------------------------
# Detection result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectionResult:
    """One observation's verdict from :class:`DeadLoopDetector`."""

    plan_id: str
    task_id: str
    fingerprint: str
    progress_token: str
    count: int
    threshold: int
    triggered: bool
    reason: str
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view (for logs and the CLI)."""
        return {
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "fingerprint": self.fingerprint,
            "progress_token": self.progress_token,
            "count": self.count,
            "threshold": self.threshold,
            "triggered": self.triggered,
            "reason": self.reason,
            "observed_at": self.observed_at,
        }


@dataclass
class _PlanState:
    """Per-plan accumulator: the last observation and its repeat count."""

    fingerprint: str = ""
    progress_token: str = ""
    task_id: str = ""
    count: int = 0


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class DeadLoopDetector:
    """Accumulate consecutive identical failures per plan.

    The detector is a pure in-memory accumulator: it touches no
    filesystem and no clock other than :func:`framework.clock.utcnow_iso`
    for the observation timestamp. :class:`Watchdog` owns the I/O.
    """

    def __init__(self, threshold: int = DEADLOOP_THRESHOLD) -> None:
        if not isinstance(threshold, int) or isinstance(threshold, bool):
            raise ValueError(f"threshold must be an int; got {threshold!r}")
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1; got {threshold}")
        self._threshold = threshold
        self._states: dict[str, _PlanState] = {}

    @property
    def threshold(self) -> int:
        """The consecutive-observation count that trips a trigger."""
        return self._threshold

    def count(self, plan_id: str) -> int:
        """Return the current consecutive count for ``plan_id`` (0 if unseen)."""
        safe_id = validate_plan_id(plan_id)
        state = self._states.get(safe_id)
        return state.count if state else 0

    def reset(self, plan_id: str | None = None) -> None:
        """Clear the accumulator for one plan, or for every plan."""
        if plan_id is None:
            self._states.clear()
            return
        self._states.pop(validate_plan_id(plan_id), None)

    def record(
        self,
        plan_id: str,
        task_id: str,
        error_text: str,
        progress_token: str,
    ) -> DetectionResult:
        """Record one failed execution round and return the verdict.

        The counter advances only when BOTH the error fingerprint and
        the progress token match the previous observation for this
        plan. Any change to either resets the counter to 1 — a
        different failure, or a task that actually moved, is not the
        same dead loop.
        """
        safe_id = validate_plan_id(plan_id)
        fingerprint = compute_fingerprint(safe_id, task_id, error_text)

        state = self._states.get(safe_id)
        if (
            state is not None
            and state.fingerprint == fingerprint
            and state.progress_token == progress_token
        ):
            state.count += 1
        else:
            state = _PlanState(
                fingerprint=fingerprint,
                progress_token=progress_token,
                task_id=str(task_id),
                count=1,
            )
            self._states[safe_id] = state

        triggered = state.count >= self._threshold
        if triggered:
            reason = (
                f"dead loop: task {task_id!r} failed with the same error "
                f"fingerprint {state.count} consecutive times "
                f"(threshold {self._threshold}) with no task state progression"
            )
        else:
            reason = (
                f"task {task_id!r} repeated the same failure {state.count}/"
                f"{self._threshold} time(s) with no task state progression"
            )

        return DetectionResult(
            plan_id=safe_id,
            task_id=str(task_id),
            fingerprint=fingerprint,
            progress_token=progress_token,
            count=state.count,
            threshold=self._threshold,
            triggered=triggered,
            reason=reason,
            observed_at=utcnow_iso(),
        )


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


class Watchdog:
    """Poll one plan directory and detect dead loops.

    The watchdog tails ``plans/{plan_id}/execution.log`` — it remembers
    how many lines it has already consumed so re-polling an unchanged
    log produces no new observations (otherwise a quiet daemon would
    trip the threshold purely by being polled three times).
    """

    def __init__(
        self,
        plan_id: str,
        plans_root: Path | str,
        threshold: int = DEADLOOP_THRESHOLD,
        on_trigger: Callable[[DetectionResult], None] | None = None,
    ) -> None:
        self.plan_id = validate_plan_id(plan_id)
        self.plans_root = Path(plans_root)
        self.detector = DeadLoopDetector(threshold=threshold)
        self._on_trigger = on_trigger
        # Number of ``execution.log`` lines already folded into the
        # detector. Re-reading the file from the top on every poll is
        # cheap enough (the log rotates at 10 MB) and avoids byte-offset
        # bookkeeping that a rotation would invalidate.
        self._consumed_lines = 0
        # Guards the ``on_trigger`` hook so a plan that stays dead does
        # not re-invoke the action sequence on every subsequent poll.
        self._trigger_fired = False

    # -- Paths ---------------------------------------------------------

    @property
    def plan_dir(self) -> Path:
        """``plans/{plan_id}/``."""
        return self.plans_root / self.plan_id

    @property
    def log_file(self) -> Path:
        """``plans/{plan_id}/execution.log``."""
        return self.plan_dir / EXECUTION_LOG_FILENAME

    @property
    def tasks_file(self) -> Path:
        """``plans/{plan_id}/tasks.json``."""
        return self.plan_dir / TASKS_FILENAME

    # -- Reads ---------------------------------------------------------

    def read_progress_token(self) -> str:
        """Digest the current task snapshot.

        A missing or corrupt ``tasks.json`` yields the digest of an
        empty snapshot: the watchdog must keep detecting even when the
        task store is unreadable (an unreadable store is itself a
        plausible cause of the loop we are looking for).

        Task #3.8: ``tasks.json`` is now static-only. Runtime fields
        (``status`` / ``commit_sha``) live in the SQLite
        ``plan_execution.task_progress.tasks`` column. We overlay
        them onto the rows here so ``compute_progress_token`` still
        sees the same input shape it always has. The watchdog's
        "progress detected" logic depends on ``status`` and
        ``commit_sha`` changing between polls; without the overlay
        the digest would be stable and the dead-loop detector would
        silently fail.
        """
        try:
            data = json.loads(self.tasks_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return compute_progress_token([])
        if isinstance(data, Mapping):
            rows = data.get("tasks", [])
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        if not isinstance(rows, list):
            rows = []
        # ---- Overlay runtime state from SQLite (task #3.8) ----
        try:
            from state_machine.db.connection import open as _open_db
            from state_machine.db.schema import migrate as _migrate
            from state_machine.repositories.plan_task_repository import (
                PlanTaskRepository,
            )

            runtime_conn = _open_db(resolve_state_db_path())
            try:
                _migrate(runtime_conn)
                runtime = PlanTaskRepository(runtime_conn).load_all(
                    self.plan_id,
                )
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    rt = runtime.get(row.get("id"))
                    if not rt:
                        continue
                    for k in ("status", "commit_sha"):
                        if k in rt:
                            row[k] = rt[k]
            finally:
                runtime_conn.close()
        except Exception:
            # State-machine not on disk yet, or column unreadable —
            # the rows carry runtime fields directly (legacy plan).
            pass
        return compute_progress_token(rows)

    def _read_new_failures(self) -> list[dict[str, Any]]:
        """Return failure entries appended since the previous poll."""
        try:
            lines = self.log_file.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return []

        if len(lines) < self._consumed_lines:
            # The log rotated underneath us; restart from the top of the
            # new file rather than silently skipping its contents.
            self._consumed_lines = 0

        fresh = lines[self._consumed_lines :]
        self._consumed_lines = len(lines)

        failures: list[dict[str, Any]] = []
        for line in fresh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if not self._is_failure(entry):
                continue
            failures.append(entry)
        return failures

    @staticmethod
    def _is_failure(entry: Mapping[str, Any]) -> bool:
        """True when the log entry represents a failed execution round."""
        if entry.get("event") in FAILURE_EVENTS:
            return True
        return str(entry.get("level", "")).upper() in {"ERROR", "CRITICAL"}

    # -- Detection -----------------------------------------------------

    def detect_once(self) -> DetectionResult | None:
        """Poll once and return the latest verdict, or ``None`` if quiet.

        ``None`` means "no new failure since the last poll" — the plan
        is either healthy or simply idle. When several failures were
        appended between polls, each is folded into the detector in
        order and the LAST verdict is returned (that is the one whose
        count reflects the whole batch).
        """
        failures = self._read_new_failures()
        if not failures:
            return None

        progress_token = self.read_progress_token()
        result: DetectionResult | None = None
        for entry in failures:
            result = self.detector.record(
                self.plan_id,
                str(entry.get("task_id") or ""),
                str(entry.get("message") or ""),
                progress_token,
            )

        assert result is not None  # non-empty ``failures`` guarantees this
        if result.triggered and not self._trigger_fired:
            self._trigger_fired = True
            if self._on_trigger is not None:
                self._on_trigger(result)
        elif not result.triggered:
            # The plan recovered (different failure / real progress);
            # re-arm the hook for the next genuine dead loop.
            self._trigger_fired = False
        return result

    def run(
        self,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        max_polls: int | None = None,
    ) -> DetectionResult | None:
        """Poll until a dead loop triggers (or ``max_polls`` is reached).

        Returns the triggering :class:`DetectionResult`, or ``None`` if
        the poll budget ran out first. ``max_polls=None`` polls
        indefinitely — that is how the supervisor runs the watchdog in
        production.
        """
        polls = 0
        while max_polls is None or polls < max_polls:
            result = self.detect_once()
            if result is not None and result.triggered:
                return result
            polls += 1
            if max_polls is None or polls < max_polls:
                time.sleep(poll_interval)
        return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchdog",
        description=(
            "Independent watchdog process: detect a daemon dead loop on one "
            "plan (>= 3 consecutive identical failures with no task state "
            "progression)."
        ),
    )
    parser.add_argument("--plan-id", required=True, help="Plan id to watch.")
    parser.add_argument(
        "--plans-root",
        default="plans",
        help="Root directory containing plans/{plan_id}/ (default: plans).",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEADLOOP_THRESHOLD,
        help=f"Consecutive failures that trip a trigger (default: {DEADLOOP_THRESHOLD}).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll exactly once and exit (used by tests and by CI probes).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=(
            "Seconds between polls in long-running mode "
            f"(default: {DEFAULT_POLL_INTERVAL_SECONDS})."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Exit codes:

    * ``0`` — polled successfully, no dead loop detected.
    * ``1`` — dead loop detected (the supervisor / caller acts on it).
    * ``2`` — bad arguments (unsafe plan id, non-positive threshold).

    On exit codes 0 and 1 a single JSON object is printed to stdout so
    the caller can parse the verdict without scraping prose.
    """
    args = _build_parser().parse_args(argv)

    try:
        watchdog = Watchdog(
            args.plan_id,
            plans_root=args.plans_root,
            threshold=args.threshold,
        )
    except InvalidPlanIdError as exc:
        print(f"invalid plan_id: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"invalid threshold: {exc}", file=sys.stderr)
        return 2

    if args.once:
        result = watchdog.detect_once()
    else:
        result = watchdog.run(poll_interval=args.poll_interval)

    payload = {
        "plan_id": watchdog.plan_id,
        "threshold": watchdog.detector.threshold,
        "triggered": bool(result is not None and result.triggered),
        "count": result.count if result is not None else 0,
        "observation": result.to_dict() if result is not None else None,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 1 if payload["triggered"] else 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
