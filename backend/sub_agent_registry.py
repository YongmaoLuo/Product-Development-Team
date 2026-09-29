"""Sub-Agent Registry — tracks live verification sub-agents for HB watchdog.

Why this exists
---------------
HeartbeatMonitor (``backend/server.py``) only watches the main
verification thread via ``_lazy_check_verification``. A sub-agent
(``VerificationSubAgent`` → ``ClaudeCodingTool.query_json``) can hang
on a slow LLM provider without the main thread dying; the main loop
appears alive forever. Without intervention, the plan is stuck in
``verification_running`` indefinitely.

This module gives HeartbeatMonitor a per-VP ``last_progress_ts`` view
plus a handle to the sub-agent's underlying ``Popen``. The watchdog
can then:

  1. detect stuck sub-agents (age from last progress exceeds
     ``SUB_AGENT_STALE_THRESHOLD_SEC``, default 1200s / 20min);
  2. *kill* the stale subprocess via ``utils.process.kill_process_group``
     (SIGTERM with SIGKILL escalation), then
  3. mark the surrounding verification round as ``failed`` with
     ``stop_reason="sub_agent_did_not_progress"`` so the next API call
     reports truth.

The registry mirrors the existing module-level dict + per-key-lock
pattern used by ``_execution_state`` / ``_verification_state`` in
``backend/server.py`` (server.py:600-612, L3156-3199). Holds strong
references to ``ClaudeCodingTool`` instances whose ``_current_process``
the watchdog wants to reach.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

__all__ = ["SubAgentHandle", "SubAgentRegistry", "sub_agent_registry"]


SUB_AGENT_STALE_THRESHOLD_SEC = int(os.getenv("SUB_AGENT_STALE_THRESHOLD_SEC", "1200"))


@dataclass
class SubAgentHandle:
    """Per-VP live state the HeartbeatMonitor needs to detect + kill a stall."""

    plan_id: str
    vp_id: str
    attempt: int
    # Weak reference would be cleaner but the coding_tool's _current_process
    # access requires the strong handle to remain valid. Lifecycle is bounded
    # by the verification round, so leakage is bounded.
    scoped_tool: Optional[object] = None
    started_at: float = field(default_factory=time.time)
    last_progress_ts: float = field(default_factory=time.time)
    stage: str = "starting"
    timeout_seconds: int = 120
    max_retries: int = 0
    # 2026-09-07: counter incremented by the watchdog (under
    # ``registry._lock``) every time this sub-agent's subprocess is
    # killed due to staleness. Read by the verification thread inside
    # ``_execute_attempt`` to decide between "retry with fresh
    # subprocess" (count == 1) and "give up, mark SKIPPED" (count >=
    # 2). Defaults to 0 because most handles never hit the watchdog
    # kill path.
    watchdog_kill_count: int = 0

    def age_from_last_progress(self) -> float:
        """Seconds since ``last_progress_ts`` was last touched."""
        return time.time() - self.last_progress_ts


class SubAgentRegistry:
    """Thread-safe in-memory registry of live verification sub-agents.

    ``register_sub_agent`` is called from inside ``_execute_attempt`` after
    a scoped ``ClaudeCodingTool`` is built. ``mark_progress`` should be
    invoked on every meaningful stage transition (LLM call start,
    self-heal start, parse attempt). ``unregister`` is called from a
    ``finally`` block so the handle is always cleaned up — even when
    the watchdog kills the subprocess.

    ``find_stale`` is read by the HeartbeatMonitor (every 30s) and returns
    handles whose ``last_progress_ts`` is older than the requested
    threshold.
    """

    def __init__(self) -> None:
        self._by_plan: Dict[str, List[SubAgentHandle]] = {}
        self._lock = threading.Lock()

    # ----- mutation (caller: verification_subagent._execute_attempt) -----

    def register_sub_agent(
        self,
        plan_id: str,
        vp_id: str,
        attempt: int,
        *,
        scoped_tool: Optional[object] = None,
        timeout_seconds: int = 120,
        max_retries: int = 0,
    ) -> SubAgentHandle:
        """Register a sub-agent handle. Idempotent: re-registering the
        same (plan_id, vp_id, attempt) refreshes ``last_progress_ts``
        rather than creating duplicates."""
        now = time.time()
        with self._lock:
            handles = self._by_plan.setdefault(plan_id, [])
            for existing in handles:
                if (
                    existing.vp_id == vp_id
                    and existing.attempt == attempt
                ):
                    existing.scoped_tool = scoped_tool
                    existing.last_progress_ts = now
                    existing.started_at = now
                    existing.stage = "starting"
                    existing.timeout_seconds = timeout_seconds
                    existing.max_retries = max_retries
                    return existing
            handle = SubAgentHandle(
                plan_id=plan_id,
                vp_id=vp_id,
                attempt=attempt,
                scoped_tool=scoped_tool,
                started_at=now,
                last_progress_ts=now,
                stage="starting",
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
            )
            handles.append(handle)
            return handle

    def unregister(self, plan_id: str, handle: SubAgentHandle) -> None:
        """Remove a handle by identity. Safe to call multiple times."""
        with self._lock:
            handles = self._by_plan.get(plan_id, [])
            try:
                handles.remove(handle)
            except ValueError:
                pass
            if not handles:
                self._by_plan.pop(plan_id, None)

    def mark_progress(
        self, plan_id: str, handle: SubAgentHandle, stage: str = ""
    ) -> None:
        """Update ``last_progress_ts`` (and optionally ``stage``) for a
        handle. Called from verification_subagent whenever a meaningful
        progress checkpoint fires (attempt_started, llm_query_start,
        self_heal_start, attempt_completed).

        If ``handle`` is no longer registered (already cleaned up), this
        is a no-op — keeps call-sites in the sub-agent simple.
        """
        with self._lock:
            handles = self._by_plan.get(plan_id, [])
            if handle not in handles:
                return
            handle.last_progress_ts = time.time()
            if stage:
                handle.stage = stage

    def cleanup_for_plan(self, plan_id: str) -> None:
        """Drop all handles for a plan. Called when verification ends
        (passed / failed / loop_stopped / user_stopped) so dead
        references don't accumulate."""
        with self._lock:
            self._by_plan.pop(plan_id, None)

    # ----- read (caller: HeartbeatMonitor._check_once) -----

    def find_stale(self, threshold_seconds: int) -> List[SubAgentHandle]:
        """Return a snapshot of all handles whose
        ``time.time() - last_progress_ts > threshold_seconds``.

        Returns a *new list* (not internal storage) so the caller can
        iterate without holding the registry lock.
        """
        now = time.time()
        stale: List[SubAgentHandle] = []
        with self._lock:
            for handles in self._by_plan.values():
                for h in handles:
                    if (now - h.last_progress_ts) > threshold_seconds:
                        stale.append(h)
        return stale

    # ----- introspection (used by tests + debug) -----

    def all_handles(self, plan_id: str) -> List[SubAgentHandle]:
        """Return a snapshot of all handles for a given plan. Used by tests."""
        with self._lock:
            return list(self._by_plan.get(plan_id, []))


# Module-level singleton — HeartbeatMonitor + sub-agent code both import this.
sub_agent_registry = SubAgentRegistry()
