"""
Verification Point Splitting
============================

Pure-function module that decides whether a single verification point
should be decomposed into smaller sub-VPs when it times out. Extracted
out of the VerificationAgent so the splitting policy can be unit-tested
in isolation, with no LLM or filesystem coupling.

The decision is intentionally trivial — three rules, no state, no
side effects — because the production code path that consumes it is
already complex enough. Future enhancements (e.g. adaptive chunking by
clause length) can be added here without touching the agent.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional


class SplitDecision:
    """Decide whether and how to split a timed-out verification point.

    The class is a namespace for a single classmethod; it carries no
    instance state. Using a class (rather than a free function) gives
    callers a single importable symbol to patch in tests and makes the
    TDD spec (``SplitDecision.should_split(vp, result)``) read
    naturally at the call site.
    """

    # Default per-chunk timeout when a VP is split. 2026-09-13:
    # per-VP override deleted — sub-VPs always carry the flat 1-hour
    # cap value, matching the top-level enforcement
    # (VerificationSubAgent.HARD_WALL_CLOCK_CAP_SECONDS).
    DEFAULT_SUBTASK_TIMEOUT_SECONDS: int = 3600

    @classmethod
    def should_split(
        cls,
        vp: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> Optional[List[Dict[str, Any]]]:
        """Return a list of sub-VPs if the VP should be split, else ``None``.

        Splitting rules (all three must hold):

        1. ``result["status"]`` is ``"timeout"`` or ``"hard_timeout"``
           — only timeouts justify further decomposition; failed VPs
           are handled by the repair-task generator, not by chunking.
           (2026-09-13 bugfix: the ``HardTimeoutError`` branch in
           ``_run_single_vp_async`` passes ``status="hard_timeout"``,
           so accepting only ``"timeout"`` made this splitter dead
           code on the hard-timeout path since ff9dfbb.)
        2. ``vp["expected_result"]`` contains at least one ``;``
           separator, splitting the expectation into two or more
           sub-clauses.
        3. The clauses are non-empty after stripping whitespace —
           a stray trailing ``;`` should not produce a phantom
           sub-VP.

        The returned list carries ``id`` (``VP-XXX-1``, ``VP-XXX-2``,
        ...), ``parent_vp_id`` (so downstream code can roll results
        back up), the original ``verification_method``, the chunked
        ``expected_result``, and a fixed ``timeout_seconds`` (3600)
        so each sub-VP is independently executable.
        """
        if not isinstance(vp, Mapping) or not isinstance(result, Mapping):
            return None

        if result.get("status") not in ("timeout", "hard_timeout"):
            return None

        expected_result = vp.get("expected_result", "")
        if not isinstance(expected_result, str) or ";" not in expected_result:
            return None

        clauses = [chunk.strip() for chunk in expected_result.split(";")]
        clauses = [c for c in clauses if c]
        if len(clauses) < 2:
            return None

        parent_id = str(vp.get("id", ""))
        if not parent_id:
            return None

        method = vp.get("verification_method", "code_review")
        method_str = method if isinstance(method, str) else "code_review"

        timeout_seconds = cls.DEFAULT_SUBTASK_TIMEOUT_SECONDS

        sub_vps: List[Dict[str, Any]] = []
        for index, clause in enumerate(clauses, start=1):
            sub_vps.append(
                {
                    "id": f"{parent_id}-{index}",
                    "parent_vp_id": parent_id,
                    "verification_method": method_str,
                    "expected_result": clause,
                    "timeout_seconds": timeout_seconds,
                }
            )
        return sub_vps