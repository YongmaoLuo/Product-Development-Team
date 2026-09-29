"""
Regression tests for the ``files_to_modify`` parallel-scheduling fix.

Background
----------
``SubTask._validate_files_to_modify`` (task.py) used to map BOTH
``None`` (missing field) AND ``[]`` (explicit empty list) to the
``UNKNOWN_MODIFICATIONS_SENTINEL`` value, on the assumption that
"empty == I don't know what this task touches". The dispatcher then
serialised every sentinel-flavoured task into its own micro-layer
(``_build_micro_layers``, agent.py) — pessimistically safe but
catastrophic for parallelism when a plan mixes read-only
("调研 / 定位 / 文档") tasks with code-implementation tasks: the
read-only tasks each got their own private micro-layer and never
shared the layer with anything else, so the whole layer ran solo.

The fix splits the two cases:

  * ``None`` → still sentinel (legacy safety net for un-migrated
    on-disk ``tasks.json`` files where the field is absent).
  * ``[]`` → empty list preserved. ``_build_micro_layers`` treats
    ``[]`` as "no declared file conflicts" and batches the task
    with other conflict-free tasks in the same micro-layer.

The tests below pin this contract end-to-end:

  1. ``test_empty_list_preserved_through_subtask`` — explicit ``[]``
     stays as ``[]`` (not sentinel) after ``SubTask`` validation.
  2. ``test_missing_field_still_uses_sentinel`` — the legacy safety
     net keeps firing for un-migrated plans.
  3. ``test_empty_list_tasks_batch_in_micro_layer`` —
     ``_build_micro_layers`` batches 5 read-only tasks + 1
     file-declaring task into the same micro-layer instead of
     splitting them into 6 separate serial layers.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def test_empty_list_preserved_through_subtask():
    """Explicit ``files_to_modify=[]`` must NOT be coerced to the
    sentinel. Plan authors who explicitly declare a read-only task
    should get the lightweight "no file conflict" semantics."""
    from task import SubTask, UNKNOWN_MODIFICATIONS_SENTINEL

    st = SubTask(
        id="1", title="调研任务", description="read-only", test_command="true",
        files_to_modify=[],
    )
    assert st.files_to_modify == [], (
        f"explicit empty list was coerced to {st.files_to_modify!r}; "
        f"the dispatcher would then serialise this task in its own "
        f"micro-layer instead of batching it with other conflict-free "
        f"tasks."
    )
    assert st.files_to_modify != UNKNOWN_MODIFICATIONS_SENTINEL


def test_missing_field_still_uses_sentinel():
    """The legacy safety net must keep firing for un-migrated
    ``tasks.json`` files that omit ``files_to_modify`` entirely.
    Otherwise we'd silently change conflict-graph semantics for
    every pre-existing plan."""
    from task import SubTask, UNKNOWN_MODIFICATIONS_SENTINEL

    st = SubTask(
        id="1", title="legacy task", description="no ftm declared",
        test_command="true",
    )
    # No files_to_modify in the kwargs — the model's default_factory
    # substitutes the sentinel.
    assert st.files_to_modify == UNKNOWN_MODIFICATIONS_SENTINEL, (
        "missing field must keep falling back to the sentinel so "
        "un-migrated plans still serialise unknown tasks defensively."
    )


def test_empty_list_tasks_batch_in_micro_layer():
    """Five read-only tasks and one file-declaring task should all
    land in the same micro-layer (no file conflicts between them),
    so the dispatcher can run them concurrently via asyncio.gather."""
    from agent import _build_micro_layers
    from task import SubTask

    tasks = [
        SubTask(id=str(i), title=f"task {i}", description="x",
                test_command="true", files_to_modify=[])
        for i in range(1, 6)
    ]
    tasks.append(
        SubTask(id="6", title="modify", description="y",
                test_command="true", files_to_modify=["backend/agent.py"])
    )

    micro = _build_micro_layers(tasks)

    # All six tasks should land in a single micro-layer because none
    # of the empty-files tasks declare a conflict, and the one
    # file-declaring task's single path is shared by nothing.
    assert len(micro) == 1, (
        f"expected 1 micro-layer (parallel), got {len(micro)}: "
        f"{[[t.id for t in m] for m in micro]}. Multiple layers means "
        f"the dispatcher would run them serially via sequential gather "
        f"passes, killing the concurrency the fix is meant to recover."
    )
    assert sorted(t.id for t in micro[0]) == [
        "1", "2", "3", "4", "5", "6",
    ]


def test_sentinel_tasks_can_run_in_parallel():
    """The sentinel ``__UNKNOWN_MODIFICATIONS__`` value is
    self-describing read-only / pure-investigation per the
    2026-09-09 simplified rule. Three sentinel tasks share no
    files (sentinel is not a real path), so they can run in
    parallel in the SAME micro-layer — no need to serialise.

    Previously this test asserted the legacy strict behaviour
    (one micro-layer per sentinel task) under the assumption
    that sentinel meant "unknown files, may race on real
    files". With the simplified rule, sentinel is read-only by
    author intent, so parallel execution is safe.
    """
    from agent import _build_micro_layers
    from task import SubTask

    sentinel_tasks = [
        SubTask(id=str(i), title=f"t{i}", description="x",
                test_command="true")
        for i in range(1, 4)
    ]

    micro = _build_micro_layers(sentinel_tasks)
    # All three sentinel tasks can run in a single micro-layer
    # because they share no real file paths (the sentinel value is
    # not a filesystem path).
    assert len(micro) == 1, (
        f"sentinel (read-only) tasks should run in parallel within "
        f"one micro-layer; got {len(micro)}: "
        f"{[[t.id for t in m] for m in micro]}"
    )
    assert sorted(t.id for t in micro[0]) == ["1", "2", "3"]