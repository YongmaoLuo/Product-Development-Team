"""
Execution Profile Generator
===========================

Builds a static execution profile from a verification plan: total
estimated wall-clock duration, per-method groups, per-method timeouts,
parallelism cap, and any sub-task splits that have already been
scheduled. Extracted out of the VerificationAgent so the projection can
be unit-tested in isolation and reused (e.g. for the "verification
budget" card on the bridge) without re-walking the plan.

The generator is a pure value calculator: no I/O, no LLM, no
filesystem. The only input is the plan dict (or a list of
verification points) and an optional :class:`TimeoutPolicy`. The only
output is a JSON-serializable dict.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List, Mapping, Optional, Union

from verification_config import (
    DEFAULT_GLOBAL_TIMEOUT_SECONDS,
    DEFAULT_PARALLELISM_CAP,
    DEFAULT_PER_METHOD_TIMEOUT_SECONDS,
    TimeoutPolicy,
)


class ExecutionProfileGenerator:
    """Build an execution profile for a verification plan.

    The generator partitions the plan's verification points by
    ``verification_method`` and estimates the wall-clock duration of
    each group as ``count * per_method_timeout`` (sequential, with the
    parallelism cap only mattering at orchestration time). Sub-task
    splits that have already been recorded on individual VPs (via
    :class:`SplitDecision`) are surfaced as a flat list so the
    orchestrator can decide whether to schedule them.
    """

    def __init__(
        self,
        plan: Union[Mapping[str, Any], List[Mapping[str, Any]], None] = None,
        timeout_policy: Optional[TimeoutPolicy] = None,
    ) -> None:
        if timeout_policy is None:
            timeout_policy = TimeoutPolicy.defaults()
        self._timeout_policy = timeout_policy
        self._verification_points: List[Dict[str, Any]] = self._extract_points(plan)
        self._subtask_splits: List[Dict[str, Any]] = []
        self._total_duration_sec: int = 0
        self._group_profiles: List[Dict[str, Any]] = []
        self._built: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self) -> Dict[str, Any]:
        """Compute the profile and return it as a JSON-friendly dict.

        Calling ``build`` more than once is allowed and is idempotent:
        the second call returns the cached result without re-walking the
        verification points. ``record_subtask_split`` calls invalidate
        the cache so a split added after the initial build is
        reflected on the next call.
        """
        if not self._built:
            self._recompute()
            self._built = True
        return self.to_dict()

    def record_subtask_split(self, parent_vp_id: str, sub_vp_ids: List[str]) -> None:
        """Record a single SplitDecision outcome on the profile.

        The split is appended to ``subtask_splits`` (preserving order)
        and the cached profile is invalidated so the next ``build``
        call re-projects the totals.

        Args:
            parent_vp_id: The original VP id (e.g. ``"VP-001"``).
            sub_vp_ids: The list of sub-VP ids produced by
                :meth:`SplitDecision.should_split`.
        """
        if not sub_vp_ids:
            return
        self._subtask_splits.append(
            {
                "parent_vp_id": parent_vp_id,
                "sub_vp_ids": list(sub_vp_ids),
            }
        )
        self._built = False

    def to_dict(self) -> Dict[str, Any]:
        """Return the cached profile as a plain dict (no recomputation).

        Schema (additive — the legacy ``group_profiles`` key is preserved
        so old consumers do not break):

        - ``total_duration_sec`` (``int``)
        - ``group_profiles`` (``list``) — legacy, retained verbatim
        - ``groups`` (``list``) — alias of ``group_profiles`` keyed for
          the bridge UI which expects the shorter name
        - ``current_group_index`` (``int``) — index of the group the
          orchestrator is currently running. ``0`` when no run has
          started; updated at runtime by the orchestrator.
        - ``subtask_splits`` (``list``)
        - ``per_method_timeouts`` (``dict``)
        - ``parallelism_cap`` (``int``)
        """
        group_list = [dict(g) for g in self._group_profiles]
        return {
            "total_duration_sec": int(self._total_duration_sec),
            "group_profiles": group_list,
            "groups": group_list,
            "current_group_index": 0,
            "subtask_splits": [dict(s) for s in self._subtask_splits],
            "per_method_timeouts": dict(self._effective_per_method_timeouts()),
            "parallelism_cap": int(self._timeout_policy.parallelism_cap),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_points(
        plan: Union[Mapping[str, Any], List[Mapping[str, Any]], None],
    ) -> List[Dict[str, Any]]:
        """Normalise the plan input into a list of VP dicts.

        Accepts:
        - A plan dict with a ``verification_points`` key.
        - A bare list of VP dicts.
        - ``None`` (returns ``[]``).
        """
        if plan is None:
            return []
        if isinstance(plan, Mapping):
            raw = plan.get("verification_points", [])
        else:
            raw = plan
        if not isinstance(raw, list):
            return []
        normalised: List[Dict[str, Any]] = []
        for item in raw:
            if isinstance(item, Mapping):
                normalised.append(dict(item))
        return normalised

    def _effective_per_method_timeouts(self) -> "OrderedDict[str, int]":
        """Return a stable ordered mapping of method → timeout (seconds).

        Order: every method that actually appears in the plan comes
        first (in first-seen order), followed by the hard-coded
        defaults for any method that did not appear, so the profile
        always carries a complete picture without depending on
        iteration order of an internal dict.
        """
        seen: "OrderedDict[str, int]" = OrderedDict()
        for vp in self._verification_points:
            method = str(vp.get("verification_method", "")).strip()
            if not method or method in seen:
                continue
            seen[method] = int(self._timeout_policy.resolve(method))

        for method, value in DEFAULT_PER_METHOD_TIMEOUT_SECONDS.items():
            if method not in seen:
                seen[method] = int(value)

        # Always include the global default as a final entry for
        # orchestration code that wants a single fallback.
        seen.setdefault("__global__", int(self._timeout_policy.global_default()))
        return seen

    def _recompute(self) -> None:
        """Walk the plan and rebuild the per-group + total duration."""
        buckets: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
        for vp in self._verification_points:
            method = str(vp.get("verification_method", "")).strip() or "code_review"
            buckets.setdefault(method, []).append(vp)

        group_profiles: List[Dict[str, Any]] = []
        total = 0
        for method, members in buckets.items():
            timeout_sec = int(self._timeout_policy.resolve(method))
            duration_sec = timeout_sec * len(members)
            total += duration_sec
            group_profiles.append(
                {
                    "method": method,
                    "count": len(members),
                    "timeout_seconds": timeout_sec,
                    "duration_seconds": duration_sec,
                    "vp_ids": [str(m.get("id", "")) for m in members if m.get("id")],
                }
            )

        # If the plan is empty, still surface a zero-count profile so
        # downstream consumers can render an "empty" state without
        # special-casing the missing key.
        if not group_profiles:
            group_profiles.append(
                {
                    "method": "code_review",
                    "count": 0,
                    "timeout_seconds": int(DEFAULT_GLOBAL_TIMEOUT_SECONDS),
                    "duration_seconds": 0,
                    "vp_ids": [],
                }
            )

        self._group_profiles = group_profiles
        self._total_duration_sec = total