"""从 verification_report.json 提取 failed VPs 的本地代码 reader。

Why this module exists
----------------------
The repair generator (``backend/repair_generator.py``) previously
contained a three-step LLM chain that *never* read the on-disk
``verification_report.json`` to find failed VPs. Instead it asked the
LLM to re-derive the failures from PRD context, evidence, and
requirement deviations — which round-trips the LLM without ever giving
it the actual ``actual_result`` / ``evidence`` text that the
verification agent already produced.

That bug meant the repair loop silently produced zero repair tasks (the
LLM had no concrete failure to anchor on), so the plan got stuck at
"verification_failed → no repair → stuck".

This module is the local-code reader that closes the gap. It is
deliberately:

  * **LLM-free** — pure disk I/O + JSON parse. The state machine can
    rely on the returned list being byte-stable: same report on disk
    always yields the same list of failed VPs.
  * **Schema-aware** — the verified schema for
    ``verification_report.json`` uses ``verification_results`` (not
    ``verification_points``; that key is for
    ``verification_plan.json``). The two are different files and have
    different keys — see the cross-references in
    ``repair_generator.collect_failure_evidence`` and
    ``orchestrator._extract_failed_ids`` which both read
    ``verification_results``.
  * **Defensive** — every I/O is in its own try/except so a missing or
    corrupt file degrades to an empty list, never a crash. Callers
    decide how to react.

The reader is consumed by
:meth:`verification.orchestrator.VerificationOrchestrator.check_cycle_conditions`
which feeds the failed-VP list into the LLM as evidence so the LLM
can produce concrete ``title / description / acceptance_criteria``
without having to guess what failed.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def extract_failed_vps_from_report(
    report_path: Path,
    plan_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Read ``verification_report.json`` and return all FAILED VPs.

    Args:
        report_path: Path to ``verification_report.json`` written by
            :meth:`verification_agent.VerificationAgent.generate_verification_report`.
        plan_path: Optional path to ``verification_plan.json``. When
            provided the reader overlays ``priority`` / ``title`` /
            ``expected_result`` / ``test_command`` from the plan onto
            each failed VP — the report schema only carries the LLM
            judgment (``actual_result`` / ``evidence``), not the test
            command that should be re-run. The orchestrator passes
            this so downstream repair tasks know which command to fix.

    Returns:
        A list of dicts, one per failed VP, each containing:

        * ``id`` — VP ID like ``"VP-034"``.
        * ``title`` — copied from the plan when available, else from
          the report. Empty string when neither file has it.
        * ``priority`` — ``"high"`` / ``"medium"`` / ``"low"`` from
          the plan; defaults to ``"medium"``.
        * ``actual_result`` — what the verification observed.
        * ``evidence`` — concrete evidence the verdict was based on
          (test output, screenshot path, etc.).
        * ``test_command`` — only present when ``plan_path`` is given
          and the plan has it.
        * ``expected_result`` — only present when ``plan_path`` is
          given and the plan has it.

        Returns an empty list when the report is missing, unreadable,
        or contains no FAILED entries. Callers MUST handle the empty
        case explicitly (e.g. abort the repair round with a clear log
        line, not crash).

    Note:
        ``verification_report.json`` uses ``verification_results`` as
        its per-VP key — NOT ``verification_points``. That key belongs
        to ``verification_plan.json``. Confirmed by reading the actual
        production report at
        ``plans/2026-09-04/verification_report.json``
        on 2026-09-07.
    """
    if not report_path.exists():
        return []

    try:
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    plan: Dict[str, Dict[str, Any]] = {}
    if plan_path is not None and plan_path.exists():
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan_data = json.load(f)
            plan = {
                str(vp.get("id", "")): vp
                for vp in (plan_data.get("verification_points") or [])
                if vp.get("id")
            }
        except (OSError, json.JSONDecodeError):
            # Plan overlay is best-effort; an unreadable plan still
            # produces a usable report-derived list.
            plan = {}

    # Build the union of result lists: top-level field + last round
    # snapshot. The list of failed-VP entries is appended with a
    # ``id`` dedupe check so the same VP appearing in both (during
    # the snapshot+clear race) doesn't double-count.
    results_lists: List[List[Dict[str, Any]]] = []
    top_level = report.get("verification_results") or []
    if top_level:
        results_lists.append(top_level)
    rounds_field = report.get("rounds") or []
    if isinstance(rounds_field, list) and rounds_field:
        # Take the LAST round snapshot — that holds the most recent
        # failed set after a round_start snapshot+clear.
        last_round = rounds_field[-1] if isinstance(rounds_field[-1], dict) else {}
        last_results = last_round.get("results") or []
        if isinstance(last_results, list) and last_results:
            # Always include the last round snapshot — the ``id``
            # dedupe below handles the case where top-level and
            # rounds[-1].results overlap.
            results_lists.append(last_results)

    failed: List[Dict[str, Any]] = []
    for results_list in results_lists:
        for r in results_list:
            if not isinstance(r, dict):
                continue
            if r.get("status") != "FAILED":
                continue
            vp_id = str(r.get("id", ""))
            if not vp_id:
                continue
            # Dedupe across top-level + rounds[-1].results. Top-level
            # wins as the most recent verdict (it's appended first so
            # its entry is preserved).
            if any(f.get("id") == vp_id for f in failed):
                continue
            vp_plan = plan.get(vp_id, {})
            entry: Dict[str, Any] = {
                "id": vp_id,
                "title": str(
                    r.get("title") or vp_plan.get("title", "") or ""
                ),
                "priority": str(
                    vp_plan.get("priority", "medium") or "medium"
                ),
                "actual_result": str(r.get("actual_result", "") or ""),
                "evidence": str(r.get("evidence", "") or ""),
            }
            # Plan-only fields — only included when the plan exists so the
            # caller can rely on ``"test_command" in entry`` to mean "the
            # plan had a runnable command".
            if vp_plan:
                if vp_plan.get("test_command"):
                    entry["test_command"] = str(vp_plan["test_command"])
                if vp_plan.get("expected_result"):
                    entry["expected_result"] = str(vp_plan["expected_result"])
            failed.append(entry)

    return failed


# ---------------------------------------------------------------------------
# Path-based variant (2026-09-10 plan — repair evidence without inlining)
# ---------------------------------------------------------------------------


# Long-form fields are summarised, never inlined, so the repair prompt
# stays bounded no matter how big the pytest dump is. 200 chars is enough
# to tell "245 failed, 2935 passed" apart from "connection refused" while
# costing ~1/25 of a 5 KB traceback. The full text stays reachable through
# ``evidence_paths`` — the LLM Reads the file when it needs the detail.
_SUMMARY_LIMIT = 200


def _summarize(text: Any, limit: int = _SUMMARY_LIMIT) -> str:
    """Collapse ``text`` to a single-line summary of at most ``limit`` chars."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _collect_log_paths(plan_dir: Path) -> List[str]:
    """Absolute paths of every ``logs/*.log`` under ``plan_dir``."""
    logs_dir = plan_dir / "logs"
    if not logs_dir.is_dir():
        return []
    try:
        return [
            str(p.resolve())
            for p in sorted(logs_dir.glob("*.log"))
            if p.is_file()
        ]
    except OSError:
        return []


def extract_vp_test_commands(plan_path: Path) -> Dict[str, str]:
    """Return ``{vp_id: test_command}`` for every VP in the plan.

    :func:`extract_failed_vps_with_paths` deliberately redacts the
    command into a bounded ``test_command_summary`` (and a
    ``test_command_path`` pointer) so an LLM prompt built from a failed
    VP stays bounded. That is the right shape for *reading*, but the
    repair round needs the **raw** command: it becomes the repair
    task's own ``test_command`` (see ``RepairTaskAssembler.assemble``),
    and the executor's dual-criterion completion rule needs a command
    it can actually run — a truncated summary or a JSON-pointer string
    would just fail with a shell error.

    Only non-empty string values are returned. Returns ``{}`` when the
    plan is missing or malformed — callers treat that as "no command
    available", not as an error.
    """
    if plan_path is None or not Path(plan_path).exists():
        return {}
    try:
        with open(plan_path, "r", encoding="utf-8") as f:
            plan_data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(plan_data, dict):
        return {}

    commands: Dict[str, str] = {}
    for vp in plan_data.get("verification_points") or []:
        if not isinstance(vp, dict):
            continue
        vp_id = str(vp.get("id", "") or "").strip()
        command = vp.get("test_command")
        if vp_id and isinstance(command, str) and command.strip():
            commands[vp_id] = command.strip()
    return commands


def extract_failed_vps_with_paths(
    report_path: Path,
    plan_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Path-based sibling of :func:`extract_failed_vps_from_report`.

    Same FAILED-VP selection and same plan overlay, but the long-form
    fields are **not** copied into the returned dicts. Instead each entry
    carries:

    * ``actual_result_summary`` / ``evidence_summary`` — collapsed to at
      most :data:`_SUMMARY_LIMIT` characters, so an LLM prompt built from
      the whole list stays bounded even when a VP's evidence is a 5 KB
      pytest dump.
    * ``evidence_paths`` — absolute paths the caller (an agent with a
      Read tool) can open for the full text. Report fields are addressed
      with a JSON pointer fragment
      (``<report>#/verification_results/<idx>/actual_result``), plus
      every ``logs/*.log`` under the plan directory.
    * ``test_command_path`` / ``test_command_summary`` /
      ``expected_result_summary`` — the plan overlay, same shape rule.

    Why this exists: ``extract_failed_vps_from_report`` inlines
    ``actual_result`` / ``evidence`` and the repair prompt truncates each
    to 1000 chars. For a VP whose whole point is "which of the 245 failing
    tests", that truncation throws away the actionable part. Returning
    paths instead lets the agent pull exactly the slice it needs.

    The keys ``actual_result`` and ``evidence`` are deliberately ABSENT —
    callers that want the old shape should keep using
    :func:`extract_failed_vps_from_report`.

    Returns an empty list when the report is missing, unreadable, or
    contains no FAILED entries.
    """
    if not report_path.exists():
        return []

    try:
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    plan: Dict[str, Dict[str, Any]] = {}
    if plan_path is not None and plan_path.exists():
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan_data = json.load(f)
            plan = {
                str(vp.get("id", "")): vp
                for vp in (plan_data.get("verification_points") or [])
                if vp.get("id")
            }
        except (OSError, json.JSONDecodeError):
            plan = {}

    # ``(results_list, json_pointer_prefix)`` pairs. The prefix is what
    # makes an ``evidence_paths`` entry resolvable: it points at the exact
    # array element the summary was derived from. Top-level results and
    # the last round snapshot are both walked, mirroring
    # :func:`extract_failed_vps_from_report`.
    sources: List[Any] = []
    top_level = report.get("verification_results") or []
    if isinstance(top_level, list) and top_level:
        sources.append((top_level, "/verification_results"))
    rounds_field = report.get("rounds") or []
    if isinstance(rounds_field, list) and rounds_field:
        last_round = rounds_field[-1] if isinstance(rounds_field[-1], dict) else {}
        last_results = last_round.get("results") or []
        if isinstance(last_results, list) and last_results:
            sources.append(
                (last_results, f"/rounds/{len(rounds_field) - 1}/results")
            )

    report_abs = str(report_path.resolve())
    plan_abs = str(plan_path.resolve()) if plan_path is not None else ""
    log_paths = _collect_log_paths(report_path.parent)

    failed: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for results_list, pointer_prefix in sources:
        for idx, r in enumerate(results_list):
            if not isinstance(r, dict) or r.get("status") != "FAILED":
                continue
            vp_id = str(r.get("id", ""))
            # Same dedupe rule as the inlining reader: the top-level
            # array is the most recent verdict and wins.
            if not vp_id or vp_id in seen_ids:
                continue
            seen_ids.add(vp_id)

            vp_plan = plan.get(vp_id, {})
            actual = str(r.get("actual_result", "") or "")
            verdict_evidence = str(r.get("evidence", "") or "")

            evidence_paths: List[str] = []
            if actual:
                evidence_paths.append(
                    f"{report_abs}#{pointer_prefix}/{idx}/actual_result"
                )
            if verdict_evidence:
                evidence_paths.append(
                    f"{report_abs}#{pointer_prefix}/{idx}/evidence"
                )
            evidence_paths.extend(log_paths)

            entry: Dict[str, Any] = {
                "id": vp_id,
                "title": str(r.get("title") or vp_plan.get("title", "") or ""),
                "priority": str(vp_plan.get("priority", "medium") or "medium"),
                "actual_result_summary": _summarize(actual),
                "evidence_summary": _summarize(verdict_evidence),
                "evidence_paths": evidence_paths,
            }
            if vp_plan:
                if vp_plan.get("test_command"):
                    entry["test_command_path"] = (
                        f"{plan_abs}#/{vp_id}/test_command"
                    )
                    entry["test_command_summary"] = _summarize(
                        vp_plan["test_command"]
                    )
                if vp_plan.get("expected_result"):
                    entry["expected_result_summary"] = _summarize(
                        vp_plan["expected_result"]
                    )
            failed.append(entry)

    return failed


# ---------------------------------------------------------------------------
# Round-snapshot helpers (2026-09-08 plan — fix stale Feishu card bug)
# ---------------------------------------------------------------------------


def snapshot_round_results(report_path: Path, round_number: int) -> None:
    """Move the current ``verification_results`` into ``rounds[N-1]``
    so a new round can start with a clean slate.

    Idempotent: if ``rounds[round_number-1]`` already exists, skip.
    The caller is responsible for not double-snapshotting (typically
    the orchestrator only calls this once per round start).

    Why: the Feishu card reads ``verification_results`` directly. If
    round 0 leaves a VP-034=FAILED entry there and round 1 starts
    re-running VP-034, the card shows round 0's stale verdict until
    round 1 finishes its first VP. Snapshotting into ``rounds[N-1]``
    preserves the historical data for audit while clearing the
    top-level array so the card reflects only the current round.
    """
    if not report_path.exists():
        return
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    rounds = data.setdefault("rounds", [])
    if any(r.get("round") == round_number - 1 for r in rounds):
        return  # already snapshotted this round
    prev_results = data.get("verification_results", []) or []
    if not prev_results:
        return  # nothing to snapshot (clean slate already)
    rounds.append({
        "round": round_number - 1,
        "results": prev_results,
        "snapshot_at": datetime.utcnow().isoformat() + "Z",
    })
    data["rounds"] = rounds
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def clear_round_results(report_path: Path) -> None:
    """Reset ``verification_results`` so the next round's VPs start fresh.

    The previous round's results remain in ``rounds`` for audit (see
    :func:`snapshot_round_results`). This is the second half of the
    stale-Feishu-card fix — after snapshotting, clearing the
    top-level array makes the next ``vp_complete`` event write the
    new round's results cleanly.
    """
    if not report_path.exists():
        return
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    data["verification_results"] = []
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass