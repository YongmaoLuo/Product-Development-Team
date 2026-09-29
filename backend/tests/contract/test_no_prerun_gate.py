"""Contract: there is NO execution-side pre-run gate (2026-09-21).

Everything must be validated at task-generation time: there is no
pre-run gate. Once a plan is executable, starting it should succeed
immediately.

Background
----------
``agent._load_tasks`` used to run the 4-step ``TaskOutputValidator`` over
the freshly-loaded snapshot and, when an ``auto_fix`` pass could not
repair it, write a watchdog signal and ``raise RuntimeError``. Validation
belongs at generation time; at execution time the only outcomes of a
failure are "abort a run the operator has already been told started" or
"run something known-broken", and the gate validated one snapshot against
a single run-level ``project_dir`` while every task carries its own.

A production plan died 0.4 s after a successful
``POST /api/execution/{id}/start`` on exactly that:

    RuntimeError: dispatcher post-read gate: TaskOutputValidator found 5
    unfixable failure(s) (failed_steps=[3,3,3,3,3],
    failed_task_ids=['1','4','10','12','15'])

— all five being tasks that CREATE the first file in a directory that did
not exist yet.

These tests pin the loader's new contract: it reads, repairs what it can
(dangling edges) and returns. It never validates file paths, never asks a
subagent to fill them, and never refuses to start.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    project = tmp_path / "dev-checkout"
    project.mkdir()
    (project / "native_ext" / "src").mkdir(parents=True)
    (project / "native_ext" / "src" / "core.rs").write_text(
        "// existing\n", encoding="utf-8"
    )
    # AutonomousAgent builds a GitManager eagerly — the project dir must
    # be a repo or the constructor raises before any load happens.
    import subprocess

    subprocess.run(
        ["git", "init", "-q"], cwd=project, check=True, capture_output=True
    )
    return project


def _write_tasks(project_dir: Path, tasks: list) -> None:
    import json

    (project_dir / "tasks.json").write_text(
        json.dumps({"requirement": "r", "tasks": tasks}, ensure_ascii=False),
        encoding="utf-8",
    )


def _build_agent(project_dir: Path):
    from agent import AutonomousAgent

    stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()
    return AutonomousAgent(
        requirement="no pre-run gate",
        project_dir=project_dir,
        coding_tool=stub,
        logger=None,
    )


def test_load_succeeds_for_file_to_be_created_in_missing_directory(plan_dir):
    """The exact 0921 shape: ``native_ext/`` exists, ``native_ext/docs/``
    does not, and the task is what creates it."""
    _write_tasks(plan_dir, [{
        "id": "1",
        "title": "audit",
        "description": "",
        "test_command": "echo noop",
        "files_to_modify": ["native_ext/docs/audit-0f0f0f0f.md"],
        "depends_on": [],
    }])
    agent = _build_agent(plan_dir)

    loaded = agent._load_tasks()  # must NOT raise

    assert {t.id for t in loaded} == {"1"}


def test_load_succeeds_when_a_task_declares_no_files(plan_dir):
    """Legacy task without ``files_to_modify`` — the loader substitutes
    the sentinel and carries on; nothing re-rejects it downstream."""
    _write_tasks(plan_dir, [{
        "id": "1",
        "title": "legacy",
        "description": "",
        "test_command": "echo noop",
        "depends_on": [],
    }])
    agent = _build_agent(plan_dir)

    loaded = agent._load_tasks()  # must NOT raise

    assert {t.id for t in loaded} == {"1"}


def test_gate_machinery_is_gone():
    """The gate, its self-heal loop and its watchdog writer must not come
    back by accident — their absence IS the contract now."""
    from agent import AutonomousAgent

    for attr in (
        "MAX_FILES_FILL_ATTEMPTS",
        "_identify_healable_tasks",
        "_fill_files_to_modify_via_subagent_batch",
        "_write_dispatcher_signal",
        "_warn_signal_not_written",
    ):
        assert not hasattr(AutonomousAgent, attr), (
            f"AutonomousAgent.{attr} was removed with the pre-run gate; "
            f"its return means the gate is coming back"
        )
