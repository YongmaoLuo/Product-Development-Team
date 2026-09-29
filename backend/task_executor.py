"""Task Executor — unified interface wrapper for AutonomousAgent.

This module provides :class:`TaskExecutor`, a thin adapter that exposes
the same lifecycle properties as :class:`base_executor.BaseExecutor` so
that server.py (and any other orchestrator) can treat task and
verification execution uniformly.

Design rationale
----------------
:class:`AutonomousAgent` (``agent.py``) already has its own mature
parallel scheduling (layer-based DAG execution via ``asyncio.gather`` +
:class:`ProviderConcurrencyController`).  Rather than forcing
``AutonomousAgent`` into the :class:`BaseExecutor` inheritance hierarchy
—which would require a large, risky refactor of its 26 K-token codebase—
:class:`TaskExecutor` is a **duck-typing adapter** that:

  * wraps an existing ``AutonomousAgent`` instance,
  * exposes the same ``plan_id / current_item / completed_items``
    read-only properties as :class:`BaseExecutor`,
  * delegates ``run()`` to ``AutonomousAgent.run()``.

This keeps the two executors *behaviourally* aligned (same interface
contract) without *structurally* coupling them.

Usage::

    from agent import AutonomousAgent
    from task_executor import TaskExecutor

    agent = AutonomousAgent(
        requirement=None,
        project_dir=project_dir,
        coding_tool=coding_tool,
    )
    executor = TaskExecutor(agent)
    executor.run()   # delegates to agent.run()
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent import AutonomousAgent


class TaskExecutor:
    """Duck-typing adapter exposing BaseExecutor-compatible properties."""

    def __init__(self, agent: AutonomousAgent) -> None:
        self._agent = agent

    # ------------------------------------------------------------------
    # Unified read-only API (mirrors BaseExecutor)
    # ------------------------------------------------------------------

    @property
    def plan_id(self) -> str:
        return str(self._agent.project_dir.name)

    @property
    def current_item(self) -> Optional[str]:
        """Task id currently ``in_progress``, or ``None``."""
        for t in self._agent.task_manager.tasks:
            if t.status == "in_progress":
                return t.id
        return None

    @property
    def completed_items(self) -> List[str]:
        return [
            t.id for t in self._agent.task_manager.tasks
            if t.status == "completed"
        ]

    @property
    def failed_items(self) -> List[str]:
        return [
            t.id for t in self._agent.task_manager.tasks
            if t.status == "failed"
        ]

    @property
    def skipped_items(self) -> List[str]:
        return [
            t.id for t in self._agent.task_manager.tasks
            if t.status == "skipped"
        ]

    @property
    def pending_items(self) -> List[str]:
        return [
            t.id for t in self._agent.task_manager.tasks
            if t.status == "pending"
        ]

    @property
    def all_items(self) -> List[Dict[str, Any]]:
        """Return all tasks as plain dicts (for dashboard rendering)."""
        return [t.model_dump() for t in self._agent.task_manager.tasks]

    # ------------------------------------------------------------------
    # Execution delegation
    # ------------------------------------------------------------------

    def run(self, max_parallel: Optional[int] = None) -> None:
        """Delegate to ``AutonomousAgent.run()``.

        Args:
            max_parallel: Ignored (``AutonomousAgent`` already manages
                its own concurrency via :class:`ProviderConcurrencyController`).
                Kept for interface parity with :class:`BaseExecutor.run`.
        """
        # AutonomousAgent.run() is synchronous; it internally calls
        # asyncio.run(self._run_async(...)) which already has its own
        # layer-based parallel scheduling.
        self._agent.run(max_tasks=None, timeout=None)

    # ------------------------------------------------------------------
    # Async variant (for callers that are already in an async context)
    # ------------------------------------------------------------------

    async def run_async(self, max_parallel: Optional[int] = None) -> None:
        """Async wrapper around :meth:`run`.

        Runs the synchronous ``AutonomousAgent.run()`` in a worker
        thread so the caller's event loop is not blocked.
        """
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.run, max_parallel)
