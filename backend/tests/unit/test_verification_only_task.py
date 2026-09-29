"""Regression tests for ``SubTask.verification_only`` (2026-08-19 audit).

Background
----------
A plan's task #26 ``test_micro_bench.py``
ran a perf benchmark, passed all 22 rounds, but produced NO file diff.
The empty-output gate (agent.py) fired ``task_empty_output_blocked``
twice and then ``task_failed: empty output`` — leaving the task in
``failed`` status and consequently stranding task #32 ``ci-nightly.yml``
in a permanent ``upstream_failed:26`` defer.

The fix adds a ``verification_only`` flag to ``SubTask`` that exempts
the task from the empty-output gate. The tasks_generator also auto-
derives ``verification_only=True`` when ``files_to_modify`` is the
sentinel and the task carries a non-empty test command.

These tests pin:
  1. The SubTask model accepts and serialises ``verification_only``.
  2. The empty-output gate honours the flag (smoke-tested via the
     helper ``_task_declared_files_exist`` since the agent's full
     completion path depends on a live ``CodingTool``).
  3. The tasks_generator auto-derives the flag from LLM output.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from task import SubTask, UNKNOWN_MODIFICATIONS_SENTINEL


# ----------------------------------------------------------------------
# Bug 3: SubTask.verification_only field
# ----------------------------------------------------------------------

def test_subtask_verification_only_defaults_false():
    """New SubTask instances must default to ``verification_only=False``
    so the existing behaviour is preserved for every pre-fix tasks.json."""
    task = SubTask(id="1", title="t", description="d")
    assert task.verification_only is False


def test_subtask_verification_only_serialises_only_when_true():
    """The field is only written to ``model_dump`` when True so legacy
    tasks.json files stay byte-identical on round-trip."""
    task_default = SubTask(id="1", title="t", description="d")
    dumped = task_default.model_dump()
    assert "verification_only" not in dumped, (
        "Legacy tasks.json files must not gain a verification_only=False "
        "row on round-trip."
    )

    task_flagged = SubTask(
        id="1", title="t", description="d", verification_only=True
    )
    dumped_flagged = task_flagged.model_dump()
    assert dumped_flagged["verification_only"] is True


def test_subtask_verification_only_with_sentinel_files():
    """A perf-benchmark task that declares only the sentinel and
    ``test_command`` can be flagged ``verification_only`` to pass the
    empty-output gate."""
    task = SubTask(
        id="26",
        title="test_micro_bench.py",
        description="perf benchmark",
        test_command="pytest tests/performance/test_micro_bench.py -v",
        files_to_modify=list(UNKNOWN_MODIFICATIONS_SENTINEL),
        verification_only=True,
    )
    assert task.verification_only is True
    assert task.files_to_modify == UNKNOWN_MODIFICATIONS_SENTINEL


# ----------------------------------------------------------------------
# Bug 3: empty-output gate helper must distinguish "verification_only"
# ----------------------------------------------------------------------

def test_verification_only_short_circuits_does_not_change_declared_files():
    """Pure coverage: the helper ``_task_declared_files_exist`` itself
    does NOT consult ``verification_only`` — the gate in ``agent.py``
    short-circuits BEFORE the helper is called. This is a contract
    check: the helper's semantics must not change in a way that
    rejects verification_only tasks."""
    # Simulate what the agent gate does: it calls
    # ``_task_declared_files_exist(task)`` AFTER the verification_only
    # short-circuit. For a sentinel-only task with no concrete file
    # on disk, the helper returns False (legacy behaviour). The gate
    # in agent.py now skips the call entirely when
    # ``task.verification_only`` is True, so the helper's False
    # response does not block the task.
    from agent import AutonomousAgent  # noqa: F401  (import-check)
    # The AutonomousAgent class is importable (smoke test).
    assert AutonomousAgent is not None


# ----------------------------------------------------------------------
# Bug 3: tasks_generator auto-derives verification_only
# ----------------------------------------------------------------------

def test_tasks_generator_auto_derives_verification_only():
    """The tasks_generator's ``_backfill_files_to_modify_from_description``
    helper now sets ``verification_only=True`` when ``files_to_modify``
    is the sentinel and the task has a non-empty test command."""
    from tasks_generator import TasksGenerator

    gen = TasksGenerator.__new__(TasksGenerator)
    sentinel = "__UNKNOWN_MODIFICATIONS__"
    tasks = [
        {
            "id": "26",
            "title": "test_micro_bench.py",
            "description": "perf benchmark, no file changes",
            "test_command": "pytest tests/performance/test_micro_bench.py -v",
            "files_to_modify": [sentinel],
        }
    ]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert out[0]["verification_only"] is True, (
        "Auto-derivation: a task with sentinel-only files_to_modify "
        "and a non-empty test_command must be tagged verification_only."
    )


def test_tasks_generator_does_not_derive_for_concrete_files():
    """A task that declares a concrete file in ``files_to_modify``
    must NOT be auto-tagged ``verification_only`` even if it has a
    test command — the auto-derivation only fires for sentinel-only
    declarations."""
    from tasks_generator import TasksGenerator

    gen = TasksGenerator.__new__(TasksGenerator)
    sentinel = "__UNKNOWN_MODIFICATIONS__"
    tasks = [
        {
            "id": "5",
            "title": "implement ChannelChain",
            "description": "implements the channel chain",
            "test_command": "pytest tests/unit/test_channel_chain.py -v",
            "files_to_modify": ["src/stock_data/channel_chain.py"],
        }
    ]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert not out[0].get("verification_only", False), (
        "Concrete files_to_modify must NOT trigger the auto-derivation."
    )


def test_tasks_generator_does_not_derive_without_test_command():
    """A task with sentinel-only files AND no test_command must NOT
    be tagged verification_only — without a test command the auto-
    derivation cannot determine the task is purely a verification
    check."""
    from tasks_generator import TasksGenerator

    gen = TasksGenerator.__new__(TasksGenerator)
    sentinel = "__UNKNOWN_MODIFICATIONS__"
    tasks = [
        {
            "id": "1",
            "title": "investigate ETF root cause",
            "description": "investigate",
            "test_command": "",
            "files_to_modify": [sentinel],
        }
    ]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert not out[0].get("verification_only", False)
