"""
Verification Orchestrator — pure helpers for the two-phase verification flow.

DP7 splits verification into two phases:

  Phase 1 — run every VP once (``verify_first_pass``).
  Phase 2 — re-run only the VPs that are still "unstable" based on
            the Phase-1 results plus the VP dependency graph
            (``verify_recheck``). Skipped entirely when every VP is
            stable on the first pass.

The selection logic is a pure function over the Phase-1 results and
the dependency graph, so it lives here in its own module and is
exercised by unit tests that need no LLM / I/O / fixtures.

Public surface
--------------
* ``select_unstable_vps(round1_results, vp_graph)``
    Pure helper — returns the list of VP IDs that must be re-checked
    in Phase 2. Implemented by Task 13.

* ``run_two_phase_round(vp_ids, executor, vp_graph, phase_recorder=None)``
    Single-round two-phase runner. Executes ``verify_first_pass``
    (every VP), then ``verify_recheck`` (only the unstable VPs, when
    any). Each phase entry is announced via ``phase_recorder`` so the
    caller can drive ``plan_state`` transitions.

* ``run_two_phase_loop(max_rounds, vp_ids, executor, vp_graph, ...)``
    Multi-round driver — runs ``run_two_phase_round`` up to
    ``max_rounds`` times and returns the per-round reports.

Together these helpers let the verification subsystem cap total VP
executions at ``max_rounds * 2`` (one first_pass + one recheck per
round at most), independent of how many VPs are unstable.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

# Phase-1 statuses that mark a VP as itself unstable and seed the
# downstream walk. Anything else (``PASSED``, ``SKIPPED``, ...) is
# not a propagation source.
_UNSTABLE_SEED_STATUSES = frozenset({"FAILED", "PARTIAL"})

# Phase names emitted via ``phase_recorder``. They match the new
# plan_state phases added in DP7 (``verify_first_pass`` /
# ``verify_recheck``) so the caller can transition directly.
PHASE_FIRST_PASS = "verify_first_pass"
PHASE_RECHECK = "verify_recheck"


def select_unstable_vps(
    round1_results: List[Dict[str, str]],
    vp_graph: Dict[str, List[str]],
) -> List[str]:
    """Return the VP IDs that should be re-checked in Phase 2.

    A VP is "unstable" when any of these is true:

      1. Its Phase-1 status is ``"FAILED"`` or ``"PARTIAL"``.
      2. It is downstream (directly or transitively) of an unstable
         VP — its preconditions may no longer hold, so re-running it
         could produce a different verdict.

    Parameters
    ----------
    round1_results : list of dict
        Each dict carries ``{"vp_id": str, "status": str}``. Only
        ``"FAILED"`` and ``"PARTIAL"`` seed the propagation walk.
    vp_graph : dict
        ``{vp_id: [downstream_vp_id, ...]}``. Each value is the list
        of VPs that directly depend on the key VP. Missing keys are
        treated as having no children.

    Returns
    -------
    list of str
        VP IDs that must be re-checked in Phase 2. Order is not
        guaranteed — callers that need determinism should sort.

    Edge cases (pinned by the TDD tests):

      * Empty ``round1_results`` → empty list.
      * Empty ``vp_graph`` → only the FAILED / PARTIAL VPs from
        ``round1_results``.
      * All ``"PASSED"`` → empty list (no seed).
      * ``"PARTIAL"`` is treated identically to ``"FAILED"``.
      * Downstream propagation is transitive across multiple hops.
    """
    unstable: set = set()

    # Seed: every VP that FAILED or PARTIAL'd in Phase 1 is itself
    # unstable, regardless of its graph position.
    for entry in round1_results:
        status = entry.get("status")
        if status in _UNSTABLE_SEED_STATUSES:
            vp_id = entry.get("vp_id")
            if vp_id is not None:
                unstable.add(vp_id)

    # Transitive walk over the dependency graph. ``unstable`` doubles
    # as the visited set, so each VP is added at most once even if the
    # graph contains cycles.
    queue = list(unstable)
    while queue:
        current = queue.pop()
        for child in vp_graph.get(current, []):
            if child not in unstable:
                unstable.add(child)
                queue.append(child)

    return list(unstable)


def _emit_phase(
    phase: str,
    phase_recorder: Optional[Callable[[str], None]],
) -> None:
    """Notify the caller that the orchestrator is entering ``phase``.

    Centralised so the recorder contract is enforced exactly once and
    tests can assert on the recorded sequence.
    """
    if phase_recorder is None:
        return
    phase_recorder(phase)


def _run_first_pass(
    vp_ids: List[str],
    executor: Callable[[str], str],
    phase_recorder: Optional[Callable[[str], None]],
) -> Dict[str, str]:
    """Run every VP once. Returns ``{vp_id: status}`` for Phase 1."""
    _emit_phase(PHASE_FIRST_PASS, phase_recorder)
    results: Dict[str, str] = {}
    for vp_id in vp_ids:
        results[vp_id] = executor(vp_id)
    return results


def _tag_stability(first_pass_results: Dict[str, str]) -> Dict[str, str]:
    """Tag each VP as ``"stable"`` (PASSED) or ``"unstable"`` (else)."""
    return {
        vp_id: ("stable" if status == "PASSED" else "unstable")
        for vp_id, status in first_pass_results.items()
    }


def _pick_recheck_set(
    first_pass_results: Dict[str, str],
    vp_graph: Dict[str, List[str]],
) -> List[str]:
    """Return the VP IDs to re-run in Phase 2 (sorted, deterministic)."""
    round1_payload = [
        {"vp_id": vp_id, "status": status}
        for vp_id, status in first_pass_results.items()
    ]
    return sorted(select_unstable_vps(round1_payload, vp_graph))


def run_two_phase_round(
    vp_ids: List[str],
    executor: Callable[[str], str],
    vp_graph: Dict[str, List[str]],
    phase_recorder: Optional[Callable[[str], None]] = None,
    report_writer: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Dict[str, object]:
    """Run one round of the two-phase verification loop.

    Flow:

      1. ``verify_first_pass`` — execute every VP via ``executor``.
      2. Tag each VP as ``stable`` / ``unstable`` from its Phase-1
         status.
      3. ``select_unstable_vps`` to pick the recheck set (FAILED /
         PARTIAL seeds plus their transitive downstream descendants).
      4. If the recheck set is non-empty, enter ``verify_recheck``
         and re-execute only those VPs. Otherwise skip recheck.
      5. Compute ``round1_stable_vps`` — the list of VP IDs that
         were judged ``stable`` (PASSED) on the first pass. The
         recheck phase is expected to skip exactly this set; the
         field is also written to ``verification_report.json`` so
         later passes (or audits) can see what was cached as
         stable on round 1 without re-running the executor.

    Parameters
    ----------
    vp_ids : list of str
        All VP IDs to verify this round.
    executor : callable
        ``executor(vp_id) -> status_str``. Called once per VP in
        first_pass, and once per unstable VP in recheck.
    vp_graph : dict
        ``{vp_id: [downstream_vp_id, ...]}`` — used by
        ``select_unstable_vps`` to propagate instability downstream.
    phase_recorder : callable, optional
        ``phase_recorder(phase_name) -> None``. Called once at the
        start of ``verify_first_pass`` and (when the recheck set is
        non-empty) once at the start of ``verify_recheck``. The
        caller typically uses this to drive ``plan_state``
        transitions.
    report_writer : callable, optional
        ``report_writer(report_dict) -> None``. Called once after
        the first pass has produced its stability tags but BEFORE
        the recheck phase begins. The payload carries the full
        per-round report, including ``round1_stable_vps``, so the
        caller can persist it to ``verification_report.json``.

    Returns
    -------
    dict
        ``{
            "first_pass_results": {vp_id: status},
            "recheck_results": {vp_id: status},     # only unstable VPs
            "stability_tags": {vp_id: "stable"|"unstable"},
            "round1_stable_vps": [vp_id, ...],      # PASSED on first_pass
            "entered_recheck": bool,
        }``
    """
    first_pass_results = _run_first_pass(vp_ids, executor, phase_recorder)
    stability_tags = _tag_stability(first_pass_results)

    round1_stable_vps = sorted(
        vp_id for vp_id, tag in stability_tags.items() if tag == "stable"
    )

    if report_writer is not None:
        report_writer(
            {
                "first_pass_results": dict(first_pass_results),
                "stability_tags": dict(stability_tags),
                "round1_stable_vps": list(round1_stable_vps),
            }
        )

    recheck_set = _pick_recheck_set(first_pass_results, vp_graph)
    entered_recheck = bool(recheck_set)

    recheck_results: Dict[str, str] = {}
    if entered_recheck:
        _emit_phase(PHASE_RECHECK, phase_recorder)
        for vp_id in recheck_set:
            recheck_results[vp_id] = executor(vp_id)

    return {
        "first_pass_results": first_pass_results,
        "recheck_results": recheck_results,
        "stability_tags": stability_tags,
        "round1_stable_vps": round1_stable_vps,
        "entered_recheck": entered_recheck,
    }


def run_two_phase_loop(
    max_rounds: int,
    vp_ids: List[str],
    executor: Callable[[str], str],
    vp_graph: Dict[str, List[str]],
    phase_recorder: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, object]]:
    """Run up to ``max_rounds`` two-phase rounds.

    Each round is a full ``run_two_phase_round`` (one first_pass + at
    most one recheck). The loop does not itself decide pass/fail; it
    simply executes the requested number of rounds and returns the
    per-round reports for the caller to evaluate.

    With ``max_rounds=3`` and at least one unstable VP every round,
    the total VP executions are capped at 6 (3 first_pass + 3 recheck).
    """
    if max_rounds < 1:
        return []

    reports: List[Dict[str, object]] = []
    for _ in range(max_rounds):
        report = run_two_phase_round(
            vp_ids=vp_ids,
            executor=executor,
            vp_graph=vp_graph,
            phase_recorder=phase_recorder,
        )
        reports.append(report)
    return reports
