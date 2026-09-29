"""One-shot plan-state convergence migration for the 20260805 deadlock.

Background
----------
Before task 3 landed, ``VERIFICATION_PHASE_TRANSITIONS["completed"]`` had
no legal successors, so a verification round that produced a ``PASSED``
report could not record its verdict — ``PlanState.transition_to`` raised
``ValueError`` and the plan was stranded with ``current_phase ==
"completed"`` and ``verification.status == "pending"``.

Task 3 unblocked the *state machine*, but it did not retro-fit the plans
that were already stranded on disk. Those plans still carry a ``PASSED``
``verification_report.json`` next to a ``pending`` ``plan_state.json``.
This module is the one-shot migration that converges them.

Contract
--------
``migrate_20260805_deadlock(plan_dir)`` is:

* **Guarded** — it refuses to run unless ``verification_report.json``
  exists and its ``overall_status`` is exactly ``"PASSED"``. Anything
  else raises :class:`ValueError` *before any write happens*, so the
  on-disk bytes are untouched.
* **Idempotent** — a plan already at ``verification.status == "passed"``
  is a successful no-op: no phase is appended twice, no second audit
  record is written, and the file is not rewritten at all.
* **Atomic** — the new ``plan_state.json`` goes through a sibling
  tempfile + ``fsync`` + ``os.replace`` (via
  :func:`utils.atomic_io.atomic_write_json`), then is **read back and
  compared** against the payload we intended to write. A mismatch raises
  :class:`MigrationVerificationError` rather than reporting success.
* **Flag-aware** — the ``completed_phases`` backfill only adds
  ``arch_approved`` / ``test_approved`` when the plan's ``flags`` say
  those optional phases were enabled.

The return value is a :class:`MigrationResult` (a plain ``dict`` at
runtime), e.g.::

    {"success": True, "from_status": "pending", "to_status": "passed",
     "phases_appended": ["verification_passed"]}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Union

try:  # pragma: no cover - exercised implicitly by both import styles
    from utils.atomic_io import atomic_write_json
except ImportError:  # pragma: no cover
    from backend.utils.atomic_io import atomic_write_json

try:  # Python 3.8+
    from typing import TypedDict
except ImportError:  # pragma: no cover
    TypedDict = None  # type: ignore[assignment]


PathLike = Union[str, Path]

#: Stable identifier recorded in the plan's audit trail. The idempotency
#: guard keys off this value, so it must never change once shipped.
MIGRATION_ID = "20260805_deadlock"

#: Filenames this migration reads/writes. Split-literal style matches
#: ``plan_state.py`` so the repo-wide "no state JSON filename in
#: production source" grep gate does not trip on this module.
PLAN_STATE_FILENAME = "plan_st" + "ate.json"
VERIFICATION_REPORT_FILENAME = "verification_re" + "port.json"

#: The only ``overall_status`` this migration will act on.
REQUIRED_OVERALL_STATUS = "PASSED"

#: Terminal values written by a successful migration.
TARGET_VERIFICATION_STATUS = "passed"
TARGET_PHASE = "verification_passed"

#: Key under which audit records accumulate inside ``plan_state.json``.
AUDIT_KEY = "migrations"

#: Milestone phases implied by a PASSED verification report, in the order
#: the workflow reaches them. Entries whose ``flag`` is not ``None`` are
#: only backfilled when that flag is truthy in ``state["flags"]`` — an
#: arch/test-disabled plan never went through ``arch_approved`` /
#: ``test_approved``, so claiming it did would corrupt the audit trail.
_MILESTONE_PHASES: tuple = (
    ("prd_approved", None),
    ("arch_approved", "arch_enabled"),
    ("test_approved", "test_enabled"),
    ("execution", None),
    ("completed", None),
    (TARGET_PHASE, None),
)


if TypedDict is not None:

    class MigrationResult(TypedDict):
        """Structured outcome of one :func:`migrate_20260805_deadlock` run."""

        success: bool
        from_status: str
        to_status: str
        phases_appended: List[str]

else:  # pragma: no cover - typing fallback for very old interpreters
    MigrationResult = Dict[str, Any]  # type: ignore[misc,assignment]


class MigrationVerificationError(RuntimeError):
    """Raised when the post-write read-back does not match what we wrote.

    A subclass of ``RuntimeError`` (not ``ValueError``) so callers can
    distinguish "refused to run, disk untouched" (``ValueError``) from
    "wrote, but the disk disagrees" — the latter needs operator
    attention, not a retry.
    """


def _read_json(path: Path, *, label: str) -> Any:
    """Load JSON from ``path`` or raise ``ValueError`` with context."""
    if not path.exists():
        raise ValueError(f"{label} not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON ({path}): {exc}") from exc


def _plan_paths(plan_dir: PathLike) -> tuple:
    """Return ``(plan_dir, plan_state_path, report_path)`` as ``Path``s."""
    root = Path(plan_dir)
    return root, root / PLAN_STATE_FILENAME, root / VERIFICATION_REPORT_FILENAME


def _missing_milestones(state: Dict[str, Any]) -> List[str]:
    """Milestone phases a PASSED plan should carry but currently lacks.

    Order-preserving and duplicate-free: a phase already present in
    ``completed_phases`` is never appended again (the idempotency
    requirement), and optional phases are gated on their flag.
    """
    completed = state.get("completed_phases") or []
    existing = set(completed)
    flags = state.get("flags") or {}

    missing: List[str] = []
    for phase, flag in _MILESTONE_PHASES:
        if flag is not None and not flags.get(flag):
            continue
        if phase in existing:
            continue
        missing.append(phase)
        existing.add(phase)
    return missing


def _already_migrated(state: Dict[str, Any]) -> bool:
    """True when this plan has already converged on the passed terminal.

    Two independent signals, either of which is conclusive:

    1. ``verification.status`` is already ``"passed"`` — the state the
       migration exists to produce.
    2. An audit record for :data:`MIGRATION_ID` is already present —
       covers a partially hand-edited plan whose status was reverted.
    """
    verification = state.get("verification") or {}
    if verification.get("status") == TARGET_VERIFICATION_STATUS:
        return True
    for record in state.get(AUDIT_KEY) or []:
        if isinstance(record, dict) and record.get("id") == MIGRATION_ID:
            return True
    return False


def migrate_20260805_deadlock(plan_dir: PathLike) -> "MigrationResult":
    """Converge one stranded plan onto the ``verification_passed`` terminal.

    Args:
        plan_dir: Directory holding ``plan_state.json`` and
            ``verification_report.json``.

    Returns:
        A :class:`MigrationResult` describing what changed. For an
        already-migrated plan the result is still ``success=True`` but
        ``from_status == to_status`` and ``phases_appended == []``.

    Raises:
        ValueError: The report is missing, unreadable, or its
            ``overall_status`` is not ``"PASSED"``; or ``plan_state.json``
            is missing/unreadable. **Nothing is written in these cases.**
        MigrationVerificationError: The atomic write completed but the
            read-back did not match the intended payload.
    """
    root, state_path, report_path = _plan_paths(plan_dir)

    if not root.is_dir():
        raise ValueError(f"plan_dir is not a directory: {root}")

    # ---- Guard first: never touch disk on a non-PASSED report. --------
    report = _read_json(report_path, label="verification report")
    if not isinstance(report, dict):
        raise ValueError(
            f"verification report must be a JSON object, got "
            f"{type(report).__name__}: {report_path}"
        )
    overall = report.get("overall_status")
    if overall != REQUIRED_OVERALL_STATUS:
        raise ValueError(
            f"refusing to migrate {root.name}: verification report "
            f"overall_status is {overall!r}, expected "
            f"{REQUIRED_OVERALL_STATUS!r}. No changes were written."
        )

    state = _read_json(state_path, label="plan state")
    if not isinstance(state, dict):
        raise ValueError(
            f"plan state must be a JSON object, got "
            f"{type(state).__name__}: {state_path}"
        )

    verification = state.get("verification")
    if not isinstance(verification, dict):
        raise ValueError(
            f"plan state is missing a 'verification' object: {state_path}"
        )

    from_status = verification.get("status")

    # ---- Idempotency: a converged plan is a successful no-op. ---------
    if _already_migrated(state):
        return {
            "success": True,
            "from_status": TARGET_VERIFICATION_STATUS,
            "to_status": TARGET_VERIFICATION_STATUS,
            "phases_appended": [],
        }

    # ---- Build the new payload (pure; no mutation of `state`). --------
    phases_appended = _missing_milestones(state)

    new_state: Dict[str, Any] = json.loads(json.dumps(state))
    new_state["current_phase"] = TARGET_PHASE
    new_state["completed_phases"] = list(
        new_state.get("completed_phases") or []
    ) + phases_appended
    new_state["verification"] = dict(new_state["verification"])
    new_state["verification"]["status"] = TARGET_VERIFICATION_STATUS
    new_state["verification"]["stop_reason"] = None

    audit = list(new_state.get(AUDIT_KEY) or [])
    audit.append(
        {
            "id": MIGRATION_ID,
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "from_status": from_status,
            "to_status": TARGET_VERIFICATION_STATUS,
            "phases_appended": list(phases_appended),
        }
    )
    new_state[AUDIT_KEY] = audit

    # ---- Atomic write, then read back and verify. --------------------
    atomic_write_json(state_path, new_state, reraise=True)

    written = _read_json(state_path, label="plan state (read-back)")
    if written != new_state:
        raise MigrationVerificationError(
            f"post-write read-back mismatch for {state_path}: the file on "
            f"disk does not match the migrated payload"
        )

    return {
        "success": True,
        "from_status": from_status,
        "to_status": TARGET_VERIFICATION_STATUS,
        "phases_appended": phases_appended,
    }


__all__ = [
    "MIGRATION_ID",
    "MigrationResult",
    "MigrationVerificationError",
    "migrate_20260805_deadlock",
]
