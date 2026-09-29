"""
Retry Manager
=============

Manages retry logic for failed tasks.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class RetryState:
    """Tracks retry state for a task."""
    task_id: str
    attempt_count: int = 0
    last_error: str = ""
    last_attempt_time: Optional[datetime] = None
    retry_history: List[Dict] = field(default_factory=list)


class RetryManager:
    """Manages retry logic for failed tasks."""

    # 2026-09-16: this attribute is a FALLBACK ONLY — it is no longer
    # the source of truth for the ``Attempt X of N`` text.
    #
    # The 2026-09-11 fix assumed ``config.max_retries`` was 5 (the
    # ``AgentConfig`` dataclass default in ``config.py``) and hard-coded
    # ``MAX_RETRIES = 5`` to match. That assumption was wrong: the value
    # the executor actually runs with comes from
    # ``ConfigRegistry.get('coding')`` (``config_registry.py:239``,
    # ``max_retries=2``), so the loop ran 2 attempts while the prompt
    # told the subagent it had 5 — the mirror image of the bug the
    # 2026-09-11 fix was written to remove.
    #
    # The durable fix is structural rather than another constant flip:
    # ``_execute_task_with_retry`` passes its own bound into
    # :meth:`get_retry_prompt_modifier`, so the rendered text can never
    # disagree with the loop again regardless of which config source
    # wins. This attribute survives only for callers that do not pass a
    # bound (see ``tests/test_retry_manager.py``).
    MAX_RETRIES = 5

    #: Default rendered bound when the caller does not supply one.
    DEFAULT_RETRY_BOUND = 5

    def __init__(self):
        self._retry_states: Dict[str, RetryState] = {}

    def should_retry(self, task_id: str, error: str) -> bool:
        """
        Determine if task should be retried.

        Args:
            task_id: Task identifier
            error: Error message from last attempt

        Returns:
            True if task should be retried, False otherwise
        """
        state = self._get_or_create_state(task_id)
        return state.attempt_count < self.MAX_RETRIES

    def record_attempt(self, task_id: str, error: str, success: bool):
        """
        Record an attempt for tracking.

        Args:
            task_id: Task identifier
            error: Error message (empty if success)
            success: Whether the attempt succeeded
        """
        state = self._get_or_create_state(task_id)

        if not success:
            state.attempt_count += 1
            state.last_error = error

        state.last_attempt_time = datetime.utcnow()
        state.retry_history.append({
            "attempt": state.attempt_count,
            "error": error,
            "success": success,
            "timestamp": state.last_attempt_time.isoformat()
        })

    def get_retry_prompt_modifier(
        self, task_id: str, max_retries: Optional[int] = None
    ) -> str:
        """
        Generate prompt modifier based on retry history.

        Args:
            task_id: Task identifier
            max_retries: The bound the caller's retry loop will actually
                run. ``AutonomousAgent._execute_task_with_retry`` passes
                its own ``max_retries`` here so the rendered ``of N`` can
                never disagree with the loop. Callers that omit it fall
                back to :attr:`MAX_RETRIES`.

        Returns:
            Prompt modifier string to help guide the next attempt
        """
        state = self._retry_states.get(task_id)
        if not state or state.attempt_count == 0:
            return ""

        try:
            bound = int(max_retries) if max_retries else self.MAX_RETRIES
        except (TypeError, ValueError):
            bound = self.MAX_RETRIES
        if bound < 1:
            bound = self.MAX_RETRIES

        modifier_parts = [f"\n\n[RETRY CONTEXT - Attempt {state.attempt_count + 1} of {bound}]"]

        # 2026-09-11: the empty-diff case is the most common
        # failure mode of this branch and a generic
        # "please try a different approach" hint gives the subagent
        # nothing actionable. Detect the sentinel prefix and emit a
        # targeted hint that tells the subagent exactly which tool to
        # use and what to verify (``git diff --stat`` before claiming
        # done). The sentinel prefix is set in
        # ``backend/agent.py`` (the empty-diff hard-fail branch).
        if state.last_error.startswith("empty_diff_no_changes"):
            modifier_parts.append(
                f"Previous attempt produced no file changes:\n{state.last_error}"
            )
            modifier_parts.append(
                "\nACTION REQUIRED — you must actually modify source files.\n"
                "  1. Use the Edit tool (preferred — find/replace) or "
                "Write tool on the file(s) listed in this task's "
                "``files_to_modify``. Do NOT only write commentary.\n"
                "  2. If the file does not exist yet, use Write to create "
                "it. If it exists, prefer Edit.\n"
                "  3. After editing, run ``git diff --stat`` yourself and "
                "confirm the files you expected to change appear in the "
                "output. If the diff is still empty, your edit did not "
                "land — investigate before retrying.\n"
                "  4. Then re-run the test command and report "
                "TEST_RESULT: PASSED or FAILED on its own line."
            )
        elif state.attempt_count <= 3:
            # Early retries: provide error context
            modifier_parts.append(f"Previous attempt failed with error:\n{state.last_error}")
            modifier_parts.append("\nPlease analyze the error and try a different approach.")
        else:
            # Later retries: suggest simplification
            modifier_parts.append(f"Multiple attempts have failed. Last error:\n{state.last_error}")
            modifier_parts.append("\nPlease consider:")
            modifier_parts.append("1. Breaking down the task into simpler steps")
            modifier_parts.append("2. Using a completely different approach")
            modifier_parts.append("3. Checking for environmental issues (missing dependencies, permissions)")

        # Add history summary for later attempts
        if state.attempt_count >= 2 and len(state.retry_history) > 1:
            modifier_parts.append("\n\nSummary of previous attempts:")
            for i, hist in enumerate(state.retry_history[-3:], 1):  # Last 3 attempts
                modifier_parts.append(f"\nAttempt {hist['attempt']}: {'Success' if hist['success'] else 'Failed'}")
                if not hist['success'] and hist['error']:
                    modifier_parts.append(f"  Error: {hist['error'][:200]}...")

        return "\n".join(modifier_parts)

    def get_state(self, task_id: str) -> Optional[RetryState]:
        """
        Get retry state for a task.

        Args:
            task_id: Task identifier

        Returns:
            RetryState or None if not found
        """
        return self._retry_states.get(task_id)

    def reset_state(self, task_id: str):
        """
        Reset retry state for a task.

        Args:
            task_id: Task identifier
        """
        if task_id in self._retry_states:
            del self._retry_states[task_id]

    def clear_all(self):
        """Clear all retry states."""
        self._retry_states.clear()

    def _get_or_create_state(self, task_id: str) -> RetryState:
        """Get or create retry state for a task."""
        if task_id not in self._retry_states:
            self._retry_states[task_id] = RetryState(task_id=task_id)
        return self._retry_states[task_id]
