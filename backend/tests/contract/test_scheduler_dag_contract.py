"""Contract tests for the scheduler DAG dependency-readiness gate.

This contract pins the upstream-status check that decides whether a
task is **ready** to be scheduled. It is consumed by the dispatcher's
per-tick loop (and by any layer-rebuild helper) — when the gate
returns False, the dispatcher must NOT mark the task ``in_progress``
even if the layer contains it.

Contracts:

  1. A task with no ``depends_on`` is **ready** by definition (root
     task in the DAG).
  2. A task whose ``depends_on`` is empty list (``[]``) is also
     ready by definition (no upstream).
  3. A task is **ready** iff every entry in ``depends_on`` refers to
     a task whose ``status`` is in ``{"completed", "skipped"}``.
  4. A task whose upstream is in ``{"failed", "in_progress",
     "pending"}`` is **NOT ready**, and the gate must report *which*
     upstream caused the rejection (via a structured reason).
  5. A task whose ``depends_on`` references a missing task (not in
     the task list) is **NOT ready** with reason
     ``missing_upstream``. This is a defensive guard — the
     ``_validate_dependencies`` validator catches this at plan-load
     time, but the runtime gate must still be safe.

The implementation lives in ``backend/agent.py`` as
``is_dependency_ready(task, all_tasks)`` -> ``tuple[bool, str]``
where the second element is the empty string (when ready) or a
machine-readable reason like ``upstream_failed:1-1`` /
``upstream_in_progress:2`` / ``upstream_pending:3`` /
``missing_upstream:X``. The structured reason is what the
dispatcher surfaces to the user / watchdog signal.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the backend module importable when this test file is run from
# the project root (not from ``backend/``).
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent import is_dependency_ready  # noqa: E402
from task import SubTask  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str,
    status: str = "pending",
    depends_on: list | None = None,
) -> SubTask:
    """Construct a minimal ``SubTask`` for testing.

    We avoid the heavy ``description`` / ``title`` machinery by passing
    only what ``is_dependency_ready`` actually reads: ``id``,
    ``status``, ``depends_on``.
    """
    return SubTask(
        id=task_id,
        title=f"task {task_id}",
        description=f"task {task_id} description",
        status=status,
        depends_on=list(depends_on or []),
    )


# ---------------------------------------------------------------------------
# Contracts 1 & 2: a task with no / empty depends_on is ready.
# ---------------------------------------------------------------------------


def test_ready_when_no_depends_on_attribute() -> None:
    """A task that does not declare depends_on is a root — always ready.

    Mirrors the contract used by ``_validate_dependencies``: a missing
    ``depends_on`` attribute is treated as having no upstream.
    """
    task = _make_task("root")
    # Defensive: explicitly clear the attribute to simulate a SubTask
    # whose JSON omitted ``depends_on`` entirely.
    object.__setattr__(task, "depends_on", [])
    ready, reason = is_dependency_ready(task, all_tasks=[task])
    assert ready is True, f"expected root task to be ready, got reason={reason!r}"
    assert reason == "", f"reason must be empty on success, got {reason!r}"


def test_ready_when_depends_on_empty_list() -> None:
    """Explicit empty ``depends_on`` is the same as missing: ready."""
    task = _make_task("root", depends_on=[])
    ready, reason = is_dependency_ready(task, all_tasks=[task])
    assert ready is True
    assert reason == ""


# ---------------------------------------------------------------------------
# Contract 3: ready iff every upstream is in {"completed", "skipped"}.
# ---------------------------------------------------------------------------


def test_ready_when_all_upstream_completed() -> None:
    """Two completed upstreams → task is ready."""
    upstream_a = _make_task("1", status="completed")
    upstream_b = _make_task("2", status="completed")
    task = _make_task("3", depends_on=["1", "2"])
    all_tasks = [upstream_a, upstream_b, task]
    ready, reason = is_dependency_ready(task, all_tasks=all_tasks)
    assert ready is True
    assert reason == ""


def test_ready_when_upstream_skipped() -> None:
    """An upstream with status=skipped is treated as success (PRD)."""
    upstream = _make_task("1", status="skipped")
    task = _make_task("2", depends_on=["1"])
    ready, reason = is_dependency_ready(task, all_tasks=[upstream, task])
    assert ready is True
    assert reason == ""


def test_ready_when_mixed_completed_and_skipped() -> None:
    """Mixed completed + skipped upstream → ready (both are success)."""
    upstream_a = _make_task("1", status="completed")
    upstream_b = _make_task("2", status="skipped")
    task = _make_task("3", depends_on=["1", "2"])
    ready, reason = is_dependency_ready(task, all_tasks=[upstream_a, upstream_b, task])
    assert ready is True
    assert reason == ""


# ---------------------------------------------------------------------------
# Contract 4: rejected when upstream is in {failed, in_progress, pending},
# with a structured reason naming the offending upstream id.
# ---------------------------------------------------------------------------


def test_not_ready_when_upstream_failed() -> None:
    """A failed upstream must BLOCK the downstream task, with reason
    ``upstream_failed:<id>``. This is the canonical "rejected" case
    that the gate must detect — failure does NOT propagate as a status
    label to downstream tasks (per ``_TERMINAL_TASK_STATUSES``), but
    it MUST block readiness.
    """
    upstream = _make_task("1", status="failed")
    task = _make_task("2", depends_on=["1"])
    ready, reason = is_dependency_ready(task, all_tasks=[upstream, task])
    assert ready is False
    assert "upstream_failed:1" in reason, (
        f"expected reason to name the failing upstream; got {reason!r}"
    )


def test_not_ready_when_upstream_in_progress() -> None:
    """An in-progress upstream must NOT yet allow the downstream task."""
    upstream = _make_task("1", status="in_progress")
    task = _make_task("2", depends_on=["1"])
    ready, reason = is_dependency_ready(task, all_tasks=[upstream, task])
    assert ready is False
    assert "upstream_in_progress:1" in reason, (
        f"expected reason to name the in-progress upstream; got {reason!r}"
    )


def test_not_ready_when_upstream_pending() -> None:
    """A pending upstream must block downstream execution."""
    upstream = _make_task("1", status="pending")
    task = _make_task("2", depends_on=["1"])
    ready, reason = is_dependency_ready(task, all_tasks=[upstream, task])
    assert ready is False
    assert "upstream_pending:1" in reason, (
        f"expected reason to name the pending upstream; got {reason!r}"
    )


def test_not_ready_reports_first_offending_upstream_when_mixed() -> None:
    """When multiple upstreams exist and some are not ready, the gate
    must surface **one** of the offending reasons. The exact one
    selected is implementation-defined; the contract only requires
    that the reason names an upstream with a non-success status and
    that the downstream task is rejected.
    """
    upstream_a = _make_task("1", status="completed")  # OK
    upstream_b = _make_task("2", status="failed")  # NOT OK
    upstream_c = _make_task("3", status="pending")  # NOT OK
    task = _make_task("4", depends_on=["1", "2", "3"])
    all_tasks = [upstream_a, upstream_b, upstream_c, task]
    ready, reason = is_dependency_ready(task, all_tasks=all_tasks)
    assert ready is False
    # The reason must name at least one of the offending upstreams
    # (the implementation may pick the first in declaration order or
    # the first in the all_tasks iteration order).
    assert any(
        token in reason
        for token in ("upstream_failed:2", "upstream_pending:3")
    ), f"reason did not name any offending upstream; got {reason!r}"


def test_not_ready_with_completed_plus_failed_mixed() -> None:
    """A single failed upstream is sufficient to reject, even when
    another upstream is completed. Symmetric with the all-failed
    case: any one failed upstream is the canonical rejection.
    """
    upstream_a = _make_task("1", status="completed")
    upstream_b = _make_task("2", status="failed")
    task = _make_task("3", depends_on=["1", "2"])
    ready, reason = is_dependency_ready(task, all_tasks=[upstream_a, upstream_b, task])
    assert ready is False
    assert "upstream_failed:2" in reason


# ---------------------------------------------------------------------------
# Contract 5: missing upstream is a defensive guard.
# ---------------------------------------------------------------------------


def test_not_ready_when_upstream_missing() -> None:
    """If a task references an upstream that does not exist in the
    task list, the gate must reject with ``missing_upstream:<id>``
    rather than crashing. ``_validate_dependencies`` catches this at
    plan-load time, but the runtime gate must still be safe.
    """
    task = _make_task("2", depends_on=["ghost"])
    ready, reason = is_dependency_ready(task, all_tasks=[task])
    assert ready is False
    assert "missing_upstream:ghost" in reason, (
        f"expected reason to name the missing upstream; got {reason!r}"
    )


def test_not_ready_with_multiple_missing_upstreams_reports_one() -> None:
    """With two missing upstreams, the gate reports at least one."""
    task = _make_task("3", depends_on=["ghost-a", "ghost-b"])
    ready, reason = is_dependency_ready(task, all_tasks=[task])
    assert ready is False
    assert any(
        token in reason
        for token in ("missing_upstream:ghost-a", "missing_upstream:ghost-b")
    ), f"reason did not name any missing upstream; got {reason!r}"


# ---------------------------------------------------------------------------
# Regression: a task referencing itself must be rejected, not crash.
# ---------------------------------------------------------------------------


def test_not_ready_when_self_dependency() -> None:
    """A self-dependency is rejected. The cycle is normally caught by
    ``_validate_dependencies`` at load time, but the runtime gate
    must treat the self-reference as "missing or non-terminal"
    rather than crashing.
    """
    task = _make_task("1", depends_on=["1"])
    ready, reason = is_dependency_ready(task, all_tasks=[task])
    assert ready is False
    # Either "missing_upstream" or one of the non-success status
    # reasons is acceptable — the cycle prevents the task from
    # ever observing its own completion.
    assert reason != ""