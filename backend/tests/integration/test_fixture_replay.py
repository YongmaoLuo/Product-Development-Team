"""Fixture-driven replay of the four failure dispositions.

Why this file exists
====================

Replaying the *classification* of a failure proves almost nothing: it
shows that a function returns the string the corpus expected, and every
one of those strings could be written down in a golden file and compared
byte for byte. What the acceptance criteria actually ask for is the
**disposition** — where each of the four failure classes lands, and what
the landing looks like on disk:

============================  ==========================  =============================
class                         disposition                 the side effect that proves it
============================  ==========================  =============================
``infra``                     ``retry_not_counted``       the attempt was retried
``criterion_defect``          ``triage_then_obsolete_``   the criterion was replaced
                              ``replace``                 on disk
``out_of_scope``              ``hitl_created``            a decision row exists
``deliverable_defect``        ``repair_task_created``     a repair task exists
============================  ==========================  =============================

Those four side effects live in four different modules — the attempt
retry budget in ``verification_subagent``, the criterion replacement in
``verification_plan_delta`` via the orchestrator, the decision in
``hitl.service`` and the repair task in ``repair_generator`` — so
replaying the class alone cannot reach any of them. This file drives
the product from a corpus case to the side effect, and asserts the
side effect *and* the side effects that must **not** exist (a repair
task for a retired criterion, a decision row for a deliverable defect).

What the replay drives
======================

* the **loop** — ``VerificationOrchestrator.start_verification_cycle`` and
  ``check_cycle_conditions``, driven with the report the corpus case
  describes;
* the **decision** — ``hitl.service.create_decision``, for the class
  whose remedy is to ask a human;
* the **attempt** — ``VerificationSubAgent._self_heal_loop``, for the
  class whose remedy is to retry a malfunctioned attempt.

What the replay supplies
========================

The corpus (``tests/fixtures/hitl/cases/``) ships the verdict, the
failure history and the verification plan of each case. It does **not**
ship the triage answer, so the replay carries a recorded one — the
"recorded provider" the test design asks for. The recorded answer is
built from the case itself:

* the class and the evidence quote come from the case's verdict;
* the proposed replacement, when the case states one, is that proposal
  verbatim (so the product's strength guard is the thing under test);
* when the case states none, the recorded answer proposes the same
  check at the same strength — the only proposal the product's guard
  accepts, and the one the "obsolete and replace" disposition needs.

The product does the routing
==========================

The replay does not route anything itself. It writes the report the
corpus case describes, hands it to the orchestrator, and reads what the
product did with it:

* the class the report recorded reaches the repair gate through
  ``verification_report_reader.extract_failed_vps_with_paths`` — the
  reader carries it, so the gate can tell a broken deliverable from an
  out-of-scope check;
* the class reaches the loop's own routing through
  ``verification.failure_triage.disposition_for``, so the four classes
  buy the four remedies the table names;
* an out-of-scope check becomes a question for a human through
  ``hitl.service.create_decision`` — the product asks it, on the spot,
  and the replay only reads the row that was written.

Determinism
===========

The same case replayed twice must produce byte-identical artifacts, so
the replay writes into a fresh temporary workspace each time, points the
backend at it (``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH``) and reports
only what does not vary: no timestamps, no random ids, no absolute
paths, and no dictionary whose iteration order could leak into the
result.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

import pytest


# Make ``backend/`` importable regardless of the test runner's entry
# point (``verification`` is a package under ``backend/``).
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from hitl.store import HitlStore  # noqa: E402
from verification.verification_report_reader import (  # noqa: E402
    extract_failed_vps_with_paths,
)
from verification import failure_triage  # noqa: E402
from verification import VerificationOrchestrator  # noqa: E402
from verification.triage_agent import TriageConclusion  # noqa: E402


#: Where the corpus lives. The path is derived from this file, so the
#: replay runs the same wherever the repository is checked out.
CASES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "hitl" / "cases"

#: The four dispositions, spelled the way the disposition table spells
#: them. These are the *loop-level* names — what the plan did — and not
#: the triage-level ones the corpus's ``expected_disposition`` carries
#: (``retry_counted`` / ``escalate_counted`` / ``skip_not_counted``).
DISPOSITION_RETRY = "retry_not_counted"
DISPOSITION_REPLACE = "triage_then_obsolete_replace"
DISPOSITION_ASK = "hitl_created"
DISPOSITION_REPAIR = "repair_task_created"

#: Which loop each triage disposition buys. The table is read from
#: :mod:`verification.failure_triage`, so a change there moves the
#: replay — which is exactly what the negative control relies on.
_ROUTE_BY_DISPOSITION = {
    failure_triage.Disposition.RETRY_NOT_COUNTED: "retry",
    failure_triage.Disposition.RETRY_COUNTED: "repair",
    failure_triage.Disposition.ESCALATE_COUNTED: "replace",
    failure_triage.Disposition.SKIP_NOT_COUNTED: "ask_human",
}

_DISPOSITION_BY_ROUTE = {
    "retry": DISPOSITION_RETRY,
    "repair": DISPOSITION_REPAIR,
    "replace": DISPOSITION_REPLACE,
    "ask_human": DISPOSITION_ASK,
}

#: How many rounds each route is replayed for, and why.
#:
#: * ``retry`` — two rounds, so "the same-cause counter does not move"
#:   is a statement about a *repeat* and not about a single failure;
#: * ``repair`` — one round: the repair task appears on the round the
#:   deliverable failed, and a second round would void the counter the
#:   table asks to see grow;
#: * ``replace`` — two rounds: the criterion is replaced only once the
#:   same failure has repeated for the threshold's worth of rounds;
#: * ``ask_human`` — one round: the question is asked about the failure
#:   that raised it, and the counter must not have moved.
_ROUNDS_BY_ROUTE = {"retry": 2, "repair": 1, "replace": 2, "ask_human": 1}

#: The corpus case that stands for each of the four classes, and the
#: disposition that case has to reach.
FOUR_CLASSES = (
    ("infra", "infra_429", DISPOSITION_RETRY),
    ("criterion_defect", "criterion_schema_rejected", DISPOSITION_REPLACE),
    ("out_of_scope", "out_of_scope_env_missing", DISPOSITION_ASK),
    ("deliverable_defect", "deliverable_schema_mismatch", DISPOSITION_REPAIR),
)

#: The remaining corpus cases: the same classes, the other specimens.
#: Each one has to reach the same disposition as its class.
OTHER_CASES = (
    ("infra_watchdog", DISPOSITION_RETRY),
    ("out_of_scope_scope_change", DISPOSITION_ASK),
)

#: The spellings a corpus case may use for the outcome its class buys.
#:
#: The corpus carries two vocabularies and both are honoured here. The
#: first batch pins the **triage-level** literal — the one
#: :mod:`verification.failure_triage` hands out — while the second batch
#: pins a **loop-level** literal in its own spelling (``hitl_pending``
#: for the question that waits for a human, ``repair_task_generated``
#: for the repair task). The replay reports a third spelling — the
#: disposition table's (``hitl_created``, ``repair_task_created``) —
#: because that is the table this file asserts against.
#:
#: The corpus's own integrity tests own the check that each case's
#: spelling is the one its batch requires; what matters here is that a
#: case never names the outcome of a *different* class.
_CORPUS_DISPOSITION_NAMES = {
    failure_triage.INFRA: frozenset({
        failure_triage.Disposition.RETRY_NOT_COUNTED,
    }),
    failure_triage.CRITERION_DEFECT: frozenset({
        failure_triage.Disposition.ESCALATE_COUNTED,
    }),
    failure_triage.OUT_OF_SCOPE: frozenset({
        failure_triage.Disposition.SKIP_NOT_COUNTED,
        "hitl_pending",
    }),
    failure_triage.DELIVERABLE_DEFECT: frozenset({
        failure_triage.Disposition.RETRY_COUNTED,
        "repair_task_generated",
    }),
}

#: Every case in the corpus. The determinism test walks all of them.
ALL_CASES = (
    "criterion_schema_rejected",
    "criterion_weaker_replacement",
    "deliverable_schema_mismatch",
    "infra_429",
    "infra_watchdog",
    "out_of_scope_env_missing",
    "out_of_scope_scope_change",
)


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


def _load_case(name: str) -> Dict[str, Any]:
    """The corpus case, read the way the brief's input example reads it."""
    path = CASES_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The recorded answers (the LLM stand-ins)
# ---------------------------------------------------------------------------


class _RecordedJudge:
    """The triage judge, recorded.

    The corpus ships the failure, not the answer to "what *is* this
    failure". The replay carries the answer, built from the case: the
    class and the quote come from the verdict, and the proposed
    replacement comes from the case's own point when it states one.
    """

    def __init__(self, case: Dict[str, Any], point: Dict[str, Any]) -> None:
        self._conclusion = _recorded_conclusion(case, point)
        self.calls: List[Dict[str, Any]] = []

    def judge(self, vp, failure_history, round_number=0):
        self.calls.append({
            "vp_id": str(vp.get("id", "") or "") if isinstance(vp, dict) else "",
            "round_number": round_number,
            "history_rounds": len(failure_history or []),
        })
        return self._conclusion


def _recorded_replacement(
    case: Dict[str, Any], point: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """The replacement the recorded triage answer proposes.

    The case's own proposal wins when it states one — that is the case
    the product's strength guard has to answer. When the case states
    none, the recorded answer proposes the same check at the same
    strength, which is the only proposal the guard accepts.
    """
    declared = point.get("replacement")
    if isinstance(declared, dict) and declared:
        return dict(declared)
    criterion = str(point.get("criterion") or point.get("title") or "")
    return {
        "title": criterion,
        "verification_method": str(point.get("verification_method") or ""),
        "expected_result": criterion,
    }


def _recorded_conclusion(
    case: Dict[str, Any], point: Dict[str, Any],
) -> TriageConclusion:
    """The recorded triage verdict for a case."""
    verdict = case.get("verdict") or {}
    failure_class = str(verdict.get("failure_class") or "")
    return TriageConclusion(
        vp_id=str(point.get("id") or ""),
        failure_class=failure_class,
        conclusive=True,
        evidence_quote=str(verdict.get("evidence_quote") or ""),
        rationale="the recorded triage answer for this corpus case",
        replacement=(
            _recorded_replacement(case, point)
            if failure_class == failure_triage.CRITERION_DEFECT
            else None
        ),
    )


class _RecordedCodingTool:
    """The repair-content LLM, recorded.

    The corpus ships the diff the failed attempt left behind, so the
    recorded answer declares exactly that edit scope: a scope nobody
    declared is a task the assembler refuses, and a scope the product
    derived on its own would be the product's rule, not the corpus's
    data.

    ``calls`` counts every question the product asks this model, which
    is not only the repair content: the product may ask again for the
    acceptance command of a task that declared none. The count is
    therefore read as "was the model asked at all", which is the claim
    the refusal rows make — and the only claim the count can settle.
    """

    def __init__(self, point: Dict[str, Any]) -> None:
        self.calls = 0
        self._response = {
            "tasks": [
                {
                    "failed_vp_id": str(point.get("id") or ""),
                    "title": f"[{point.get('id')}] rework the failed check",
                    "description": (
                        "the recorded failure is not a broken criterion — "
                        "rework the deliverable so the check passes"
                    ),
                    "acceptance_criteria": (
                        "the check the plan declares passes"
                    ),
                    "files_to_modify": list(point.get("diff") or []),
                },
            ],
        }

    def query_json(self, prompt=None, system_instruction=None, timeout=None,
                   **kwargs):
        self.calls += 1
        return self._response


class _GarbledVerdictTool:
    """A coding tool whose answer is not a verdict.

    The payload is what a garbled response looks like from the parser's
    side: a ``verdict`` that is a list instead of a string. The attempt
    retry budget exists for exactly this — a malfunctioned attempt —
    and the retry it earns is recorded in the attempt log.
    """

    def __init__(self) -> None:
        self.calls = 0

    def query_json(self, prompt=None, system_instruction=None, timeout=None,
                   **kwargs):
        self.calls += 1
        return {"verdict": ["FAILED"]}


# ---------------------------------------------------------------------------
# The workspace
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _hermetic(workspace: Path) -> Iterator[None]:
    """Point the backend at this replay's own workspace.

    ``PDT_PLANS_DIR`` and ``PDT_STATE_DB_PATH`` are the two seams the
    backend resolves on every call. A replay that does not move them
    writes into the repository's own ``plans/`` tree and ``state.db``.
    """
    keys = ("PDT_PLANS_DIR", "PDT_STATE_DB_PATH")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ["PDT_PLANS_DIR"] = str(workspace / "plans")
    os.environ["PDT_STATE_DB_PATH"] = str(workspace / "state.db")
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _plan_id(case: Dict[str, Any]) -> str:
    """The plan the case belongs to."""
    plan = case.get("verification_plan") or {}
    return str(plan.get("plan_id") or "") or "fixture-plan"


def _the_point(case: Dict[str, Any]) -> Dict[str, Any]:
    """The verification point the case is about.

    A case that names no point is about the *attempt* rather than about
    a check (the corpus says so: its ``verification_plan`` is empty).
    The replay still needs a point to fail — a round with no failed
    point classifies nothing — so it names one, and says so.
    """
    plan = case.get("verification_plan") or {}
    for candidate in plan.get("verification_points") or []:
        if not isinstance(candidate, dict):
            continue
        point = dict(candidate)
        point.setdefault("id", "VP-001")
        point.setdefault(
            "title", str(point.get("criterion") or point.get("id") or ""),
        )
        point.setdefault("verification_method", "code_review")
        point.setdefault(
            "expected_result", str(point.get("criterion") or ""),
        )
        return point
    return {
        "id": "VP-001",
        "title": "the attempt the corpus case is about",
        "verification_method": "code_review",
        "criterion": "the recorded attempt failed for the reason the case names",
        "expected_result": "the attempt succeeds",
    }


def _report_for(case: Dict[str, Any], point: Dict[str, Any]) -> Dict[str, Any]:
    """The round report the corpus case describes."""
    verdict = case.get("verdict") or {}
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": str(point.get("id") or ""),
                "title": str(point.get("title") or ""),
                "status": "FAILED",
                "failure_class": str(verdict.get("failure_class") or ""),
                "failure_class_source": str(
                    verdict.get("failure_class_source") or ""
                ),
                "actual_result": str(verdict.get("actual_result") or ""),
                "evidence": str(verdict.get("evidence_quote") or ""),
            },
        ],
        "requirement_deviations": [],
    }


def _write_plan_state(plan_dir: Path, plan_id: str) -> None:
    """Bootstrap the plan state the way a real plan carries it."""
    _write_json(plan_dir / "plan_state.json", {
        "plan_id": plan_id,
        "current_phase": "executing",
        "completed_phases": ["execution"],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
        },
    })


def _write_history(
    plan_dir: Path, case: Dict[str, Any], point: Dict[str, Any],
) -> None:
    """Seed the per-VP failure history the corpus case carries.

    The history is filed under the point the case is about — the same
    key the loop reads it back under. A history filed under any other
    point is a history the loop never sees.
    """
    history: Dict[str, List[Dict[str, Any]]] = {}
    vp_id = str(point.get("id") or "")
    for entry in case.get("failure_history") or []:
        if not isinstance(entry, dict):
            continue
        history.setdefault(vp_id, []).append(dict(entry))
    _write_json(
        plan_dir / "verification_failure_history.json",
        {"vp_history": history},
    )


def _verification_repository(db_path: Path, plan_id: str):
    """The verdict store the OBSOLETE verdict lands in."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db_path)
    migrate(conn)
    repo = VerificationRepository(conn)
    repo.insert(plan_id, verification_status="running", verdicts=[])
    return repo, conn


def _same_failure_counts(plan_dir: Path) -> Dict[str, int]:
    """The same-cause tally the loop persisted, as it is on disk."""
    path = plan_dir / "verification_loop_tracking.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    counts = payload.get("same_failure_counts")
    return dict(counts) if isinstance(counts, dict) else {}


def _obsolete_and_replacements(
    plan_dir: Path,
) -> tuple[List[str], Dict[str, str]]:
    """The retired criteria and the criteria that replaced them."""
    path = plan_dir / "verification_plan.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {}
    obsolete: List[str] = []
    replacements: Dict[str, str] = {}
    for point in payload.get("verification_points") or []:
        if not isinstance(point, dict):
            continue
        if point.get("obsolete"):
            obsolete.append(str(point.get("id") or ""))
        replaced = point.get("replacement_for")
        if replaced:
            replacements[str(point.get("id") or "")] = str(replaced)
    return sorted(obsolete), replacements


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------


def _run_round(
    plan_dir: Path,
    project_dir: Path,
    repo,
    report: Dict[str, Any],
    round_number: int,
    case: Dict[str, Any],
    point: Dict[str, Any],
):
    """One verification round: start the cycle, then classify it.

    Returns the outcome, the recorded judge, the recorded repair model
    and the refusals — the record of the failed points that got no
    repair task, and why. The refusals come from the product: the
    repair gate's own record plus the round's deferrals.
    """
    judge = _RecordedJudge(case, point)
    tool = _RecordedCodingTool(point)
    with patch("verification.orchestrator.VerificationAgent"):
        orchestrator = VerificationOrchestrator(
            plan_dir,
            project_dir,
            coding_tool=tool,
            verif_repo=repo,
            triage_judge=judge,
        )
        orchestrator.start_verification_cycle(round_number=round_number)
    # The (mocked) agent would have written the report; the reader reads
    # it from disk, so the replay writes it where the agent would.
    _write_json(plan_dir / "verification_report.json", report)
    outcome = orchestrator.check_cycle_conditions(
        report, round_number=round_number,
    )
    refusals = list(
        getattr(orchestrator.repair_generator, "filtered_vps", []) or []
    )
    refusals.extend(outcome.get("deferred_vps") or [])
    return outcome, judge, tool, refusals


def _decision_rows(workspace: Path, plan_id: str) -> tuple[int, int, List[Dict[str, Any]]]:
    """The questions the plan has, the ones the audit mirror records, and
    what each question is about.

    The questions are the product's own: the out-of-scope case's
    question is asked by the orchestrator, through
    ``hitl.service.create_decision``, and this only reads what landed
    in the table.

    The audit count is the number of **creations** in the mirror, not
    the number of lines: a plan with no outlet cannot deliver the
    question to anybody, so the product settles it as ``unroutable``
    right away — and that settlement is a second audit line. What the
    replay asserts is that the question was *asked*.

    The third value is each question's *subject*: the point it is
    about, the class it was raised for, the source that raised it and
    the round it came from. A question about the wrong point is a
    question nobody can answer, so the replay reads the subject rather
    than trusting the product's own account of it.
    """
    store = HitlStore(workspace / "state.db")
    try:
        decisions = store.list_by_plan(plan_id)
        rows = len(decisions)
        audit_path = store.audit_path(plan_id)
    finally:
        store.close()
    created = 0
    if audit_path.exists():
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("event") == "created":
                created += 1
    questions = [
        {
            "vp_id": str(decision.vp_id or ""),
            "failure_class": str(decision.failure_class or ""),
            "source": str(decision.source or ""),
            "round": decision.round,
            "status": str(decision.status or ""),
        }
        for decision in decisions
    ]
    return rows, created, questions


async def _no_sleep(delay, *args, **kwargs):
    """A sleep that does not sleep — the retry backoff is not the test."""
    return None


def _replay_attempt(workspace: Path, point: Dict[str, Any]) -> int:
    """Replay the malfunctioned attempt and count the retries it earned.

    The retry budget of an attempt exists for one thing: a malfunctioned
    attempt. The replay drives the loop with a payload the verdict parser
    rejects, and counts the retries the attempt log records.
    """
    from verification_subagent import VerificationSubAgent

    log_path = workspace / "attempt.log"
    sub = VerificationSubAgent(
        method=str(point.get("verification_method") or "code_review"),
        max_retries=1,
    )
    tool = _GarbledVerdictTool()
    with patch("asyncio.sleep", new=_no_sleep):
        asyncio.run(sub._self_heal_loop(
            vp_node=dict(point),
            coding_tool=tool,
            settings_path=str(workspace / "settings.json"),
            log_path=log_path,
            project_dir=None,
        ))
    events: List[Dict[str, Any]] = []
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return len([
        event for event in events
        if event.get("event") in (
            "verdict_parse_retry", "sub_agent_kill_retry",
        )
    ])


def _replay_in(
    case: Dict[str, Any],
    workspace: Path,
    route: str,
    failure_class: str,
) -> Dict[str, Any]:
    """The replay, inside an already-hermetic workspace."""
    plan_id = _plan_id(case)
    plan_dir = workspace / "plans" / plan_id
    project_dir = workspace / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)

    point = _the_point(case)
    report = _report_for(case, point)

    _write_json(plan_dir / "verification_plan.json", {
        "plan_id": plan_id,
        "verification_points": [point],
    })
    _write_plan_state(plan_dir, plan_id)
    _write_history(plan_dir, case, point)
    _write_json(plan_dir / "tasks.json", {
        "tasks": [{
            "id": "T-001",
            "title": "the work the plan verified",
            "status": "completed",
        }],
    })
    # The plan's HITL configuration: the question is asked in ``auto``
    # mode, so it carries the answer to fall back on.
    _write_json(plan_dir / "hitl_config.json", {
        "version": 1,
        "mode": "auto",
        "timeout_sec": 1800,
        "hooks": [],
    })

    repo, conn = _verification_repository(workspace / "state.db", plan_id)
    try:
        rounds: List[Dict[str, Any]] = []
        triage_calls = 0
        repair_tasks: List[Dict[str, Any]] = []
        repair_refusals: List[Dict[str, Any]] = []
        repair_llm_calls = 0
        for round_number in range(1, _ROUNDS_BY_ROUTE[route] + 1):
            outcome, judge, tool, refusals = _run_round(
                plan_dir, project_dir, repo, report, round_number, case, point,
            )
            triage_calls += len(judge.calls)
            repair_tasks = list(outcome.get("repair_tasks") or [])
            repair_refusals.extend(refusals)
            repair_llm_calls += tool.calls
            rounds.append({
                "round": round_number,
                "should_stop": bool(outcome.get("should_stop")),
                "status": str(outcome.get("status") or ""),
                "counts": _same_failure_counts(plan_dir),
            })
        obsolete, replacements = _obsolete_and_replacements(plan_dir)
        verdicts = (repo.current(plan_id) or {}).get("verdicts") or []
    finally:
        conn.close()

    # The questions the product asked. Nothing here creates one: the
    # out-of-scope case's question is asked by the orchestrator, and
    # this only reads the row it wrote.
    hitl_rows, audit_rows, questions = _decision_rows(workspace, plan_id)

    retries = 0
    if route == "retry":
        retries = _replay_attempt(workspace, point)

    return {
        "disposition": _DISPOSITION_BY_ROUTE[route],
        "triage_disposition": failure_triage.disposition_for(failure_class),
        "failure_class": failure_class,
        "rounds": rounds,
        "same_failure_counts": rounds[-1]["counts"] if rounds else {},
        "triage_calls": triage_calls,
        "obsolete": obsolete,
        "replacements": replacements,
        "obsolete_verdicts": [
            dict(verdict) for verdict in verdicts
            if isinstance(verdict, dict)
        ],
        "repair_tasks": repair_tasks,
        "repair_refusals": repair_refusals,
        "repair_llm_calls": repair_llm_calls,
        "hitl_rows": hitl_rows,
        "audit_rows": audit_rows,
        "questions": questions,
        "retries": retries,
    }


def replay_case(case: Dict[str, Any]) -> Dict[str, Any]:
    """Replay one corpus case and report what the product did with it.

    Args:
        case: A corpus case, as ``json.loads(path.read_text())`` reads it.

    Returns:
        The disposition the case's class buys, and the side effects that
        prove it: the retired criteria and their replacements, the
        repair tasks, the decision rows, the audit lines, the attempt
        retries and the same-cause tally, round by round.
    """
    verdict = case.get("verdict") or {}
    failure_class = str(verdict.get("failure_class") or "")
    triage_disposition = failure_triage.disposition_for(failure_class)
    route = _ROUTE_BY_DISPOSITION[triage_disposition]

    workspace = Path(tempfile.mkdtemp(prefix="pdt-fixture-replay-"))
    try:
        with _hermetic(workspace):
            return _replay_in(case, workspace, route, failure_class)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# The assertions
# ---------------------------------------------------------------------------


def _assert_disposition(result: Dict[str, Any], expected: str) -> None:
    """The disposition assertion — the FIRST thing every row asserts.

    It is first on purpose. The negative control has to fail *here*, so
    that a broken disposition table is located at the disposition and
    not at one of the side effects further down.
    """
    assert result["disposition"] == expected, (
        f"disposition: expected {expected!r}, got "
        f"{result['disposition']!r} (failure class "
        f"{result['failure_class']!r}, triage disposition "
        f"{result['triage_disposition']!r})"
    )


def _assert_side_effects(result: Dict[str, Any], expected: str) -> None:
    """The side effects the disposition must — and must not — have."""
    if expected == DISPOSITION_RETRY:
        assert result["retries"] > 0, (
            "the attempt was retried: the retry count must be observable, "
            f"got {result['retries']}"
        )
        assert all(
            not round_["counts"] for round_ in result["rounds"]
        ), (
            "an infrastructure blip does not move the same-cause counter, "
            f"got {result['rounds']}"
        )
        _assert_no_repair(result, failure_triage.INFRA)
        assert result["hitl_rows"] == 0, (
            "an infrastructure blip is not a question for a human, so no "
            f"decision row may exist; got {result['hitl_rows']}"
        )
        assert result["obsolete"] == [], (
            "an infrastructure blip retires no criterion, so no criterion "
            f"may be obsolete; got {result['obsolete']}"
        )
        assert result["obsolete_verdicts"] == [], (
            "an infrastructure blip records no OBSOLETE verdict, so none "
            f"may exist; got {result['obsolete_verdicts']}"
        )
        return

    if expected == DISPOSITION_REPLACE:
        assert result["obsolete"], (
            "the broken criterion is retired, so the plan must say so; "
            f"got {result['obsolete']}"
        )
        assert result["replacements"], (
            "the replacement criterion must point back at the one it "
            f"replaces; got {result['replacements']}"
        )
        replaced = set(result["obsolete"])
        for new_id, old_id in result["replacements"].items():
            assert old_id in replaced, (
                f"the replacement {new_id} points at {old_id}, which the "
                "plan does not retire"
            )
        _assert_no_repair(result, failure_triage.CRITERION_DEFECT)
        assert result["hitl_rows"] == 0, (
            "a replaced criterion is not a question for a human, so no "
            f"decision row may exist; got {result['hitl_rows']}"
        )
        assert result["obsolete_verdicts"], (
            "the plan and the verdict store must agree that the criterion "
            "is gone, so an OBSOLETE verdict must exist"
        )
        # The counter grew towards the threshold and the key was removed
        # when the criterion was retired.
        assert result["rounds"][0]["counts"], (
            "the same cause counts before the threshold is reached; got "
            f"{result['rounds'][0]['counts']}"
        )
        assert not result["same_failure_counts"], (
            "the retired criterion's key is removed from the tally; got "
            f"{result['same_failure_counts']}"
        )
        return

    if expected == DISPOSITION_ASK:
        assert result["hitl_rows"] == 1, (
            "the question is put to a human, so exactly one decision row "
            f"must exist; got {result['hitl_rows']}"
        )
        assert result["audit_rows"] == 1, (
            "the question is mirrored to the audit log, so exactly one "
            f"creation must be recorded; got {result['audit_rows']}"
        )
        # The question has to be about the point that raised it — a
        # question about the wrong point is a question nobody can
        # answer.
        questions = result["questions"]
        assert len(questions) == 1, (
            f"exactly one question must exist; got {questions}"
        )
        question = questions[0]
        assert question["vp_id"], (
            "the question must name the point it is about; got "
            f"{question}"
        )
        assert question["failure_class"] == failure_triage.OUT_OF_SCOPE, (
            "the question must be raised for the out-of-scope class; got "
            f"{question}"
        )
        assert question["source"] == "verification", (
            "the question must come from the verification path, so the "
            f"loop can tell it from any other; got {question}"
        )
        _assert_no_repair(result, failure_triage.OUT_OF_SCOPE)
        assert result["obsolete"] == [], (
            "an out-of-scope check retires no criterion, so none may be "
            f"obsolete; got {result['obsolete']}"
        )
        assert result["obsolete_verdicts"] == [], (
            "an out-of-scope check records no OBSOLETE verdict, so none may "
            f"exist; got {result['obsolete_verdicts']}"
        )
        assert all(
            not round_["counts"] for round_ in result["rounds"]
        ), (
            "an out-of-scope check does not take part in the same-cause "
            f"counter, so the tally stays empty; got {result['rounds']}"
        )
        return

    if expected == DISPOSITION_REPAIR:
        assert result["repair_tasks"], (
            "the deliverable is wrong, so a repair task must exist; got "
            f"{result['repair_tasks']}"
        )
        failed_vp_ids = {
            str(task.get("failed_vp_id") or "") for task in result["repair_tasks"]
        }
        assert result["repair_llm_calls"] >= 1, (
            "a deliverable defect is the one failure the repair model is "
            f"asked about; got {result['repair_llm_calls']} calls"
        )
        assert result["repair_refusals"] == [], (
            "a deliverable defect is not refused by the repair gate; got "
            f"{result['repair_refusals']}"
        )
        assert result["same_failure_counts"], (
            "a deliverable defect counts against the same-cause counter, so "
            f"the tally must not be empty; got {result['same_failure_counts']}"
        )
        assert all(
            count >= 1 for count in result["same_failure_counts"].values()
        ), (
            "the counter grows with the rounds the failure repeated; got "
            f"{result['same_failure_counts']}"
        )
        assert result["hitl_rows"] == 0, (
            "a wrong deliverable is not a question for a human, so no "
            f"decision row may exist; got {result['hitl_rows']}"
        )
        assert result["obsolete"] == [], (
            "a wrong deliverable retires no criterion, so none may be "
            f"obsolete; got {result['obsolete']}"
        )
        assert result["obsolete_verdicts"] == [], (
            "a wrong deliverable records no OBSOLETE verdict, so none may "
            f"exist; got {result['obsolete_verdicts']}"
        )
        assert failed_vp_ids, (
            "the repair task must name the point it repairs; got "
            f"{result['repair_tasks']}"
        )
        return

    raise AssertionError(f"unknown disposition {expected!r}")


def _assert_no_repair(result: Dict[str, Any], failure_class: str) -> None:
    """No repair task, and the repair model was never asked for one.

    The two halves are one claim seen from two sides. The empty task
    list alone would prove nothing: the assembler refuses a task with
    an empty edit scope, so a failure the model was never asked about
    also leaves no task behind. The **call count** is what says the
    gate refused the point *before* the model was asked — the rule the
    product states for itself — and the refusal the gate recorded names
    the class it refused the point for.
    """
    assert result["repair_llm_calls"] == 0, (
        f"a {failure_class} failure must not reach the repair model at "
        f"all; got {result['repair_llm_calls']} calls"
    )
    assert result["repair_tasks"] == [], (
        f"a {failure_class} failure is not a broken deliverable, so no "
        f"repair task may exist; got {result['repair_tasks']}"
    )
    refused = [
        str(refusal.get("failure_class") or "")
        for refusal in result["repair_refusals"]
    ]
    assert failure_class in refused, (
        f"the repair gate must record that it refused the point for "
        f"{failure_class}; got {result['repair_refusals']}"
    )


def _assert_row(name: str, expected: str) -> Dict[str, Any]:
    """Replay a case and assert its disposition and its side effects."""
    result = replay_case(_load_case(name))
    _assert_disposition(result, expected)
    _assert_side_effects(result, expected)
    return result


def _without_volatile_ids(value: Any) -> Any:
    """The result with the one field that is allowed to differ, masked.

    The id of a question is a uuid: the product's business is to be
    *different* every time it asks, so two identical replays are
    allowed to disagree about it — and about nothing else. What the
    question *is* is compared instead, by the point it is about and the
    class it was raised for (``_decision_rows``).

    A repair task also carries the directory it really ran in, and
    that directory is a fresh temporary one per replay: it is
    environment-derived, not part of what the artifact *is*. So it is
    masked the same way — what is compared is that the field is there
    and that every other field agrees.
    """
    if isinstance(value, dict):
        return {
            key: (
                "<a-question-id>"
                if key == "decision_id" and isinstance(item, str)
                else (
                    "<project-dir>"
                    if key == "project_dir" and isinstance(item, str)
                    else _without_volatile_ids(item)
                )
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_volatile_ids(item) for item in value]
    return value


def _artifact(result: Dict[str, Any]) -> bytes:
    """The replay's artifacts, as the bytes that have to be identical."""
    return json.dumps(
        _without_volatile_ids(result),
        ensure_ascii=False, sort_keys=True, indent=2,
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. The four dispositions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure_class,name,expected", FOUR_CLASSES,
    ids=[f"{cls}-{name}" for cls, name, _ in FOUR_CLASSES],
)
def test_four_dispositions(failure_class, name, expected):
    """Each class reaches its disposition, with its side effects.

    The table, one row per class:

    * ``infra`` — the attempt is retried, the same-cause counter does
      not move, and no repair task, no decision row and no OBSOLETE
      verdict exist;
    * ``criterion_defect`` — the criterion is retired on disk, the
      replacement points back at it, no repair task is built for it and
      the tally loses the key;
    * ``out_of_scope`` — one decision row and one audit line, no repair
      task, no OBSOLETE verdict, and the counter does not take part;
    * ``deliverable_defect`` — a repair task naming the point, the
      counter grows, and no decision row and no OBSOLETE verdict exist.
    """
    case = _load_case(name)
    assert case["verdict"]["failure_class"] == failure_class, (
        f"{name} is not a {failure_class} case"
    )
    # The corpus names the outcome its class buys — in its own batch's
    # vocabulary, which is not the vocabulary the replay reports (see
    # :data:`_CORPUS_DISPOSITION_NAMES`).
    assert case["expected_disposition"]["disposition"] in (
        _CORPUS_DISPOSITION_NAMES[failure_class]
    ), (
        f"{name} names the outcome "
        f"{case['expected_disposition']['disposition']!r}, which is not "
        f"one of the outcomes a {failure_class} buys"
    )
    assert not case["expected_disposition"]["defaulted"], (
        f"{name} claims a defaulted classification; the corpus says which "
        "class it is"
    )

    result = _assert_row(name, expected)

    assert result["triage_disposition"] == (
        failure_triage.disposition_for(failure_class)
    ), (
        "the replay must read the triage disposition off the product's "
        "own table"
    )


@pytest.mark.parametrize("name,expected", OTHER_CASES)
def test_the_other_cases_reach_the_same_disposition(name, expected):
    """The remaining corpus cases reach their class's disposition too."""
    _assert_row(name, expected)


# ---------------------------------------------------------------------------
# 2. Determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_CASES)
def test_determinism(name):
    """The same case replayed twice produces byte-identical artifacts.

    The artifacts are the disposition, the side effects and the
    generated repair tasks. Nothing in them may depend on the clock, on
    a random id or on the iteration order of a dictionary — otherwise
    two replays of one case would not be the same evidence.
    """
    case = _load_case(name)
    first = _artifact(replay_case(case))
    second = _artifact(replay_case(case))
    assert first == second, (
        f"{name} replayed twice produced different artifacts:\n"
        f"{first.decode('utf-8')}\n---\n{second.decode('utf-8')}"
    )


# ---------------------------------------------------------------------------
# 3. The negative control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ("out_of_scope_env_missing", "out_of_scope_scope_change"),
)
def test_out_of_scope_regression_red(name):
    """A broken disposition table turns the out-of-scope rows red.

    The control: ``out_of_scope`` is temporarily mapped to the
    ``repair`` disposition — a human regression, exactly the mistake the
    negative control in the test design describes. The replay then has
    to fail **at the disposition assertion**, and it has to fail for
    both out-of-scope cases. A replay that compared golden files would
    stay green here, which is why this control is the proof that the
    replay reads the product's table.
    """
    case = _load_case(name)
    with patch.dict(
        failure_triage._DISPOSITIONS,
        {
            failure_triage.OUT_OF_SCOPE: (
                failure_triage.Disposition.RETRY_COUNTED
            ),
        },
    ):
        with pytest.raises(AssertionError) as excinfo:
            _assert_row(name, DISPOSITION_ASK)

    message = str(excinfo.value)
    assert message.startswith("disposition:"), (
        "the failure must be located at the disposition assertion, not at "
        f"one of the side effects; got:\n{message}"
    )
    assert DISPOSITION_ASK in message, (
        "the failure must name the disposition that was expected; got:\n"
        f"{message}"
    )


# ---------------------------------------------------------------------------
# 4. The boundary cases
# ---------------------------------------------------------------------------


def test_a_weaker_replacement_is_refused():
    """A replacement that checks less is refused, and nothing is written.

    The corpus carries this case on purpose: the triage names the
    criterion broken, but the replacement it proposes verifies the
    criterion with a weaker method. The product's strength guard refuses
    it, so the plan is left exactly as it was — no criterion retired, no
    replacement added, no OBSOLETE verdict — and the failure keeps
    going with a fresh budget instead of being retired silently.
    """
    case = _load_case("criterion_weaker_replacement")
    point = _the_point(case)
    assert point["replacement"]["verification_method"] == "ui_validation", (
        "this case is about a replacement weaker than the one it replaces"
    )
    assert failure_triage.is_not_looser(
        point["verification_method"], point["replacement"]["verification_method"],
    ) is False, (
        "the case is only meaningful while the proposal is weaker than "
        "the criterion it replaces"
    )

    result = replay_case(case)

    assert result["obsolete"] == [], (
        "a refused replacement retires nothing; got "
        f"{result['obsolete']}"
    )
    assert result["replacements"] == {}, (
        "a refused replacement adds nothing; got "
        f"{result['replacements']}"
    )
    assert result["obsolete_verdicts"] == [], (
        "a refused replacement records no OBSOLETE verdict; got "
        f"{result['obsolete_verdicts']}"
    )
    assert result["repair_tasks"] == [], (
        "the criterion was not retired, and a criterion defect is not a "
        f"broken deliverable; got {result['repair_tasks']}"
    )
    assert result["triage_calls"] == 1, (
        "the threshold buys exactly one triage call; got "
        f"{result['triage_calls']}"
    )
    assert all(
        not round_["should_stop"] for round_ in result["rounds"]
    ), (
        "a refused replacement must not stop the chain — the failure is "
        f"still workable; got {result['rounds']}"
    )


def test_the_repair_task_carries_the_declared_scope():
    """The repair task names the files the corpus case recorded.

    The corpus carries the diff the failed attempt left behind. The
    scope has to survive the parser and the assembler: a task with an
    empty scope is refused by the assembler, so a scope that got lost
    on the way would show up as no repair task at all.
    """
    case = _load_case("deliverable_schema_mismatch")
    point = _the_point(case)
    assert point["diff"], "this case records the files the failure touched"

    result = _assert_row("deliverable_schema_mismatch", DISPOSITION_REPAIR)

    task = result["repair_tasks"][0]
    assert task["failed_vp_id"] == point["id"], (
        f"the repair task must address the failed point; got {task}"
    )
    assert list(task.get("files_to_modify") or []) == list(point["diff"]), (
        "the repair task must carry the scope the case recorded; got "
        f"{task.get('files_to_modify')}"
    )


def test_the_criterion_case_replaces_the_criterion_on_disk():
    """The plan file is the record: the old criterion is retired, the
    new one points back at it.

    The OBSOLETE verdict and the plan file have to agree, so both are
    read here — the plan file for the retirement and the pointer, the
    verdict store for the verdict the plan promises.
    """
    result = _assert_row("criterion_schema_rejected", DISPOSITION_REPLACE)

    retired = result["obsolete"]
    assert retired == ["VP-001"], (
        f"the broken criterion is VP-001; got {retired}"
    )
    replacement_ids = list(result["replacements"])
    assert len(replacement_ids) == 1, (
        f"exactly one criterion replaces the retired one; got "
        f"{result['replacements']}"
    )
    assert result["replacements"][replacement_ids[0]] == "VP-001", (
        "the replacement must point back at the criterion it replaces; got "
        f"{result['replacements']}"
    )
    verdicts = result["obsolete_verdicts"]
    assert verdicts, "the OBSOLETE verdict must exist"
    assert verdicts[0]["vp_id"] == "VP-001", (
        f"the OBSOLETE verdict must name the retired criterion; got "
        f"{verdicts[0]}"
    )
    assert verdicts[0]["replaced_by"] == replacement_ids[0], (
        "the verdict store and the plan file must name the same "
        f"replacement; got {verdicts[0]}"
    )


# ---------------------------------------------------------------------------
# 5. The reader carries the class
# ---------------------------------------------------------------------------


def test_the_reader_carries_the_failure_class_through():
    """The class the verdict recorded survives the read.

    The repair gate reads the class off the record the reader hands it
    (:func:`repair_generator.select_repairable_failed_vps`). A reader
    that dropped the class would hand the repair model every failure —
    including the out-of-scope check that is nobody's broken deliverable.

    A report that names no class at all still reads as
    ``deliverable_defect``, but the source says the class was *assumed*
    rather than earned, so a reader that guesses is not mistaken for a
    reader that knew.
    """
    workspace = Path(tempfile.mkdtemp(prefix="pdt-reader-class-"))
    try:
        report_path = workspace / "report.json"
        plan_path = workspace / "plan.json"
        _write_json(plan_path, {"verification_points": []})
        _write_json(
            report_path,
            {
                "verification_results": [
                    {
                        "id": "VP-OUT",
                        "title": "a check this plan never scoped",
                        "status": "FAILED",
                        "actual_result": "the environment never had the service",
                        "evidence": "nothing on the port",
                        "failure_class": "out_of_scope",
                        "failure_class_source": "llm",
                    },
                    {
                        "id": "VP-INFRA",
                        "title": "a check that could not run",
                        "status": "FAILED",
                        "actual_result": "the runner died",
                        "evidence": "exit 137",
                        "failure_class": "infra",
                        "failure_class_source": "llm",
                    },
                    {
                        "id": "VP-BARE",
                        "title": "a check nobody classified",
                        "status": "FAILED",
                        "actual_result": "expected 1, got 2",
                        "evidence": "assertion failed",
                    },
                ],
            },
        )

        failed = extract_failed_vps_with_paths(report_path, plan_path)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    by_id = {str(vp.get("id")): vp for vp in failed}

    assert by_id["VP-OUT"]["failure_class"] == "out_of_scope", (
        "the class the report recorded must survive the read; got "
        f"{by_id['VP-OUT']}"
    )
    assert by_id["VP-INFRA"]["failure_class"] == "infra", (
        "the class the report recorded must survive the read; got "
        f"{by_id['VP-INFRA']}"
    )
    assert by_id["VP-OUT"]["failure_class_source"] == "llm", (
        "the source the report recorded must survive the read; got "
        f"{by_id['VP-OUT']}"
    )
    assert by_id["VP-BARE"]["failure_class"] == "deliverable_defect", (
        "a report that names no class reads as the class that counts; got "
        f"{by_id['VP-BARE']}"
    )
    assert by_id["VP-BARE"]["failure_class_source"] == "defaulted", (
        "an assumed class must say so; got " f"{by_id['VP-BARE']}"
    )
