"""
Workspace Utilities
===================

Facts about a workspace the plan already chose — not a way to choose one.

This module used to also carry a filesystem scanner (``discover_workspaces``
+ ``default_search_dirs`` + ``WORKSPACE_MARKERS`` + ``format_workspace_candidates``
+ ``candidate_lookup``): it walked ``~/work``, ``~/Documents/GitHub``
and friends looking for marker files, and handed the hits to an LLM router that
assigned each task a ``project_dir``. That whole path was removed on
2026-09-22: a fabricated workspace may happen to work on one machine and be
nonsense on every other.

A rule that reads the local filesystem is meaningful on exactly one machine.
Where work happens is the plan's call (``target_project_dir`` in the PRD /
interview constraints), and for a plan that declared nothing the directory
comes from ``POST /api/execution/{id}/start``. See
``TasksGenerator.validate_workspace``.

What remains is ``detect_venv`` — a fact about the directory that was chosen,
used to make a task's ``test_command`` resolve the project's own interpreter.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def detect_venv(project_dir: Path) -> Optional[str]:
    """Look for a Python virtualenv in/around project_dir.

    Search order (first hit wins):
    1. <project_dir>/venv1/bin/activate   (the project convention)
    2. <project_dir>/venv/bin/activate
    3. <project_dir>/.venv/bin/activate  (modern convention)
    4. <project_dir>/.venv310/bin/activate / .venv311/bin/activate
    5. <project_dir_parent>/venv1/bin/activate
    6. <project_dir_parent>/.venv/bin/activate
    Returns absolute path to the activate script, or None.
    """
    candidate_dirs = [
        project_dir / "venv1",
        project_dir / "venv",
        project_dir / ".venv",
        project_dir / ".venv310",
        project_dir / ".venv311",
    ]
    if project_dir.parent and project_dir.parent != project_dir:
        candidate_dirs.extend([
            project_dir.parent / "venv1",
            project_dir.parent / "venv",
            project_dir.parent / ".venv",
        ])
    for d in candidate_dirs:
        activate = d / "bin" / "activate"
        if activate.exists():
            return str(activate)
    return None
