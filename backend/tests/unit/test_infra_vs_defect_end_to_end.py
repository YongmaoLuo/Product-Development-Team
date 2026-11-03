"""End-to-end regression: the accident's shape, from the verdict to the stop.

Background
----------
2026-11-03 (infra-vs-defect).  One round of the accident looked like
this:

* ``VP-006`` -- the **coding tool died**.  The sub-agent never came
  back with an answer ("Claude sub-process exited without assistant
  text"), so the product was **never asked**: this failure says
  something about the attempt, not about the deliverable.  The executor
  said so (``failure_class: infra``), but the judgment LLM's report has
  **no slot** for a failure class -- so the report landed on disk
  unclassified, and every consumer downstream reads an unclassified
  failure as ``deliverable_defect``: the class that counts, the class
  that buys a repair task.  The chain then spent the whole plan
  repairing a product nobody had broken, and ended as
  ``max_rounds_reached`` -- a verdict about the product, on a round
  that never judged it;
* ``VP-026`` -- a **real defect**, in the same report.  It was the
  only failure in the round that anybody could act on.

So the accident is not one failure.  It is **two failures of different
kinds in one report**, and the whole chain has to keep them apart:
the classification, the counts, the retry budget, the stop, and the
repair tasks.

What these tests pin
--------------------
 1. a coding-tool failure lands as ``infra`` -- not as a defect;
 2. ``verifier_mutated_tree`` lands as ``infra`` too: the accident's
    other shape, where the tool tried to write ``.../2>`` (a shell
    redirection read as a path) and the guard refused it;
 3. the retry ledger's exhausted budget stops the round with the
    **infra** verdict -- ``loop_stopped`` + ``infrastructure_unresolved``
    -- and not with ``max_rounds_reached``;
 4. the counts keep the two classes apart;
 5. a real defect is still repaired, and the infra point never reaches
    the repair model (the negative control for 3 and 4);
 6. the report's mislabels are corrected in **both** directions (the
    negative control for 1);
 7. the report that lands on disk keeps the classes.

The chain is driven the way the round drives it: the executor's
verdict is the authority on the class
(:meth:`Orchestrator._backfill_failure_classes`), the ledger is the
authority on the budget
(:class:`verification.infra_retry.InfraRetryLedger`), and the round's
own verdict comes from :meth:`VerificationOrchestrator.check_cycle_conditions`.

Pure rule tests: no model, no subprocess -- the only side effect is
pytest's own ``tmp_path``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import Mock, patch

import pytest


# Ensure backend/ is on sys.path so ``import verification.orchestrator``
# works regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_write_guard  # noqa: E402
from verification.infra_retry import InfraRetryLedger  # noqa: E402
from verification.orchestrator import VerificationOrchestrator  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402
from verification_persistence import VerificationPersistenceManager  # noqa: E402
from verification_subagent import Verdict  # noqa: E402


#: The terminal the report carries when the environment never let the
#: round judge a point.  Spelled out here so the test pins the code
#: itself rather than whatever the constant holds.
INFRA_UNRESOLVED_TERMINAL = "infrastructure_unresolved"

#: The reason code the retry ledger books for a coding-tool failure.
CODING_TOOL_CALL_FAILED = "coding_tool_call_failed"

#: What the coding tool said when it died.  This is the accident's
#: literal text: the sub-process left without an assistant message, so
#: there was no verdict to bring back at all.
CODING_TOOL_DEATH = (
    "coding tool call failed: Claude sub-process exited without assistant text"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_plan_dir(tmp_path):
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def mock_coding_tool():
    return Mock()


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file.

    ``PlanState`` keeps the plan's state in the ``plan_routing`` row of
    ``state.db``.  Without an override the orchestrator would write its
    ``test-plan`` row into the repo's real ``state.db`` and read back
    whatever an earlier test left behind.  Autouse, so no test in this
    module can leak into -- or inherit from -- another.
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_plan_state(plan_dir, phase="executing", verification_round=0, max_rounds=3,
                      stop_reason=None, status="pending"):
    """Helper to bootstrap plan_state.json for a test."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"] if phase not in ("interview",) else [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": status,
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": stop_reason,
        },
    }
    (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")


def _accident_execution_results() -> Dict[str, Any]:
    """What the executor returned in the accident's round.

    Two failures, two different kinds: the coding tool died on VP-006
    (the product was never asked), and VP-026 really is broken.
    """
    return {
        "execution_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "failure_class": "infra",
                "failure_class_source": "deterministic",
                "actual_result": CODING_TOOL_DEATH,
            },
            {
                "id": "VP-026",
                "status": "FAILED",
                "failure_class": "deliverable_defect",
                "failure_class_source": "defaulted",
                "actual_result": "断言未通过",
            },
        ],
    }


def _llm_report() -> Dict[str, Any]:
    """The report as the judgment LLM wrote it.

    The LLM's schema has no slot for a failure class, so the entries
    carry none.  That **is** the accident: an unclassified failure is
    read as ``deliverable_defect`` by every consumer downstream, so an
    infrastructure failure buys a repair task.
    """
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "actual_result": CODING_TOOL_DEATH,
                "evidence": {"details": "", "raw": []},
            },
            {
                "id": "VP-026",
                "status": "FAILED",
                "actual_result": "断言未通过",
                "evidence": {"details": "", "raw": []},
            },
        ],
        "requirement_deviations": [],
    }


def _infra_only_report() -> Dict[str, Any]:
    """A round in which the environment never let the round judge.

    One point, and it was **never asked**: the coding tool died and the
    retry budget was used up.  Nothing was judged, so the round has no
    verdict about the deliverable.
    """
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "actual_result": CODING_TOOL_DEATH,
                "evidence": {"details": "", "raw": []},
            },
        ],
        "requirement_deviations": [],
    }


def _run_cycle(plan_dir, project_dir, coding_tool, report) -> Tuple[Any, dict]:
    """Run one verification cycle against ``report`` and return its verdict."""
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = VerificationOrchestrator(plan_dir, project_dir, coding_tool)
        orch.start_verification_cycle(round_number=1)
        return orch, orch.check_cycle_conditions(report)


class _FakePersistence:
    """Stands in for the persistence manager the orchestrator writes through."""

    def __init__(self) -> None:
        self.events: List[Tuple[str, str, Dict[str, Any]]] = []

    def write_verification_point_log(
        self, vp_id: str, event_type: str, data: Dict[str, Any],
    ) -> None:
        self.events.append((vp_id, event_type, data))


def _agent_orchestrator(plan_dir: Path, project_dir: Path, round_no: int = 1):
    """An ``Orchestrator`` carrying only the attributes this path reads.

    ``__init__`` is skipped on purpose: it wires a coding tool, a
    persistence manager and a venv ``PATH`` prefix, none of which the
    refusal path needs.
    """
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.plan_dir = Path(plan_dir)
    orchestrator.project_dir = Path(project_dir)
    orchestrator.coding_tool = None
    orchestrator.persistence = _FakePersistence()
    orchestrator._current_round = round_no
    events: List[Tuple[str, str, Dict[str, Any]]] = []
    orchestrator._safe_log_freshness_event = (
        lambda vp_id, event_type, data: events.append((vp_id, event_type, data))
    )
    orchestrator.recorded_events = events
    return orchestrator


def _record_refusal(monkeypatch, plan_dir: Path, round_no: int,
                    vp_id: str, path: Path) -> Any:
    """Write a refusal marker the way the guard itself writes it.

    The marker file's shape is the contract between the guard (the
    writer) and the round (the reader); a hand-written marker would
    only prove that the reader can read what the reader writes.
    """
    monkeypatch.setenv(verification_write_guard.SCOPE_ENV, str(plan_dir))
    monkeypatch.setenv(verification_write_guard.VP_ID_ENV, vp_id)
    monkeypatch.setenv(verification_write_guard.ROUND_ENV, str(round_no))
    return verification_write_guard.record_refusal(
        "Bash", str(path), verification_write_guard.REASON_OUTSIDE_SCOPE,
    )


# ---------------------------------------------------------------------------
# 1. The coding tool's death is an infra failure
# ---------------------------------------------------------------------------


def test_coding_tool_failure_is_infra_end_to_end():
    """The coding tool's death lands as ``infra``, not as a defect.

    The executor ran the command and watched the sub-process die; the
    product was never asked.  The report says so -- and the counts keep
    the two failures of the accident apart, so a reader does not have to
    split them by hand.
    """
    report = _llm_report()
    report = Orchestrator._backfill_failure_classes(
        report, _accident_execution_results(),
    )
    report["failure_class_counts"] = Orchestrator._failure_class_counts(report)

    entry = report["verification_results"][0]
    assert entry["id"] == "VP-006", entry
    assert entry["failure_class"] == "infra", entry
    assert entry["failure_class_source"] == "deterministic", entry

    counts = report["failure_class_counts"]
    assert counts["infra"] == 1, counts
    assert counts["deliverable_defect"] == 1, counts


# ---------------------------------------------------------------------------
# 2. The other accident shape: a refused write is an infra failure
# ---------------------------------------------------------------------------


def test_verifier_mutated_tree_is_infra_end_to_end(monkeypatch, tmp_path):
    """``verifier_mutated_tree`` lands as ``infra`` too.

    The accident's second shape: the tool tried to write ``.../2>`` --
    a shell redirection read as a path -- and the guard refused it.
    The verdict the sub-agent handed back is about an attempt that
    malfunctioned, not about the deliverable: the answer it gave was
    given on a tree the verifier had tried to change, so the product was
    never judged.  Filed as ``infra``, it buys another attempt instead
    of a repair.
    """
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)

    orchestrator = _agent_orchestrator(plan_dir, project_dir, round_no=1)
    target = project_dir / "logs" / "2>"
    _record_refusal(
        monkeypatch, plan_dir, round_no=1, vp_id="VP-006", path=target,
    )

    verdict = Verdict(
        verdict="PASSED",
        reasons=["stub"],
        evidence=["stub evidence"],
        provider="vendor-a-pro",
        chosen_model="Vendor A-M3",
        model_complexity="simple",
    )
    rewritten = orchestrator._apply_refusal_evidence(verdict, "VP-006")

    assert rewritten.verdict == "FAILED", rewritten
    assert rewritten.failure_class == "infra", rewritten
    assert rewritten.failure_class_source == "deterministic", rewritten
    assert rewritten.reasons, "a rewritten verdict has to say why"
    assert rewritten.reasons[0].startswith("verifier_mutated_tree"), rewritten.reasons
    assert str(target) in rewritten.reasons[0], rewritten.reasons

    # The executor's projection carries the class into the report, and
    # the report is what the counts are read off.
    result = Orchestrator._verdict_to_result("VP-006", rewritten)
    assert result["failure_class"] == "infra", result
    assert result["failure_class_source"] == "deterministic", result

    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "actual_result": result["actual_result"],
                "evidence": {"details": "", "raw": []},
            },
        ],
        "requirement_deviations": [],
    }
    report = Orchestrator._backfill_failure_classes(
        report, {"execution_results": [result]},
    )
    report["failure_class_counts"] = Orchestrator._failure_class_counts(report)

    assert report["verification_results"][0]["failure_class"] == "infra", report
    assert report["failure_class_counts"]["infra"] == 1, report["failure_class_counts"]
    assert report["failure_class_counts"]["deliverable_defect"] == 0, (
        report["failure_class_counts"]
    )


# ---------------------------------------------------------------------------
# 3. The budget running out stops the round -- as the infra verdict
# ---------------------------------------------------------------------------


def test_infra_exhaustion_stops_with_infrastructure_unresolved(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """The retry budget running out stops the round -- with the infra verdict.

    The ledger has booked three attempts for VP-006, so its budget is
    used up: the environment never let this point be judged.  The round
    has to say **that** -- ``loop_stopped`` with
    ``infrastructure_unresolved`` -- and not keep iterating until the
    chain runs out of rounds.  ``max_rounds_reached`` says something
    else entirely: it says the deliverable never got through N tries.
    """
    ledger = InfraRetryLedger(temp_plan_dir)
    for round_no in (1, 2, 3):
        ledger.record_attempt(
            "VP-006", round_no,
            provider="vendor-a-pro",
            reason_code=CODING_TOOL_CALL_FAILED,
        )
    assert ledger.is_exhausted("VP-006") is True, (
        "the ledger's own budget is what decides this"
    )

    # The round asks the ledger who is out of budget -- the same way the
    # round does, off the ledger's own file.
    agent = _agent_orchestrator(temp_plan_dir, temp_project_dir, round_no=3)
    agent._infra_retry_ledger = ledger
    exhausted = agent._exhausted_infra_vp_ids()
    assert exhausted == ["VP-006"], exhausted

    report = _infra_only_report()
    report = Orchestrator._backfill_failure_classes(
        report,
        {"execution_results": [_accident_execution_results()["execution_results"][0]]},
    )
    report = Orchestrator._mark_infrastructure_unresolved(report, exhausted)
    report["failure_class_counts"] = Orchestrator._failure_class_counts(report)

    assert report["verification_results"][0]["status"] == "BLOCKED", report
    assert report["verification_results"][0]["failure_terminal"] == (
        INFRA_UNRESOLVED_TERMINAL
    ), report

    _write_plan_state(temp_plan_dir, phase="executing")
    _orch, result = _run_cycle(
        temp_plan_dir, temp_project_dir, mock_coding_tool, report,
    )

    assert result["status"] == "loop_stopped", (
        f"基础设施没解决的轮次必须以 loop_stopped 收尾，实际是 {result!r}"
    )
    assert result["stop_reason"] is None, (
        f"这一停的理由由 status 承担，不得是新字面量，实际是 {result!r}"
    )
    assert result.get("stop_reason") != "max_rounds_reached", (
        f"用完 infra 预算的轮次不是跑满轮次，实际是 {result!r}"
    )
    assert result["infrastructure_unresolved"] is True, (
        f"出口必须写明这一轮的基础设施没解决，实际是 {result!r}"
    )


# ---------------------------------------------------------------------------
# 4. The counts keep the two classes apart
# ---------------------------------------------------------------------------


def test_counts_stay_separated_and_no_repair_tasks_after_stop(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """The counts keep the classes apart, and the stop repairs nothing.

    Two halves of the same accident:

    * **the counts.**  The accident's report carries an infra failure
      and a real defect at the same time.  If the two are lumped into
      one number, the report says "2 failed" and the reader cannot
      tell which one was the environment's -- which is exactly what
      sent the loop off to repair a product nobody had broken;
    * **the stop.**  A round that stops on the infra terminal must not
      dispatch a repair task: the environment never let the round judge
      the deliverable, so there is nothing to rework.
    """
    # --- the counts, on the accident's report -------------------------
    report = _llm_report()
    report = Orchestrator._backfill_failure_classes(
        report, _accident_execution_results(),
    )
    report["failure_class_counts"] = Orchestrator._failure_class_counts(report)

    assert report["verification_results"][0]["failure_class"] == "infra", report
    assert report["failure_class_counts"] == {
        "infra": 1,
        "deliverable_defect": 1,
        "criterion_defect": 0,
        "out_of_scope": 0,
    }, report["failure_class_counts"]

    # --- the stop: the round the environment never let judge ----------
    stopped = _infra_only_report()
    stopped = Orchestrator._backfill_failure_classes(
        stopped,
        {"execution_results": [_accident_execution_results()["execution_results"][0]]},
    )
    stopped = Orchestrator._mark_infrastructure_unresolved(stopped, ["VP-006"])
    stopped["failure_class_counts"] = Orchestrator._failure_class_counts(stopped)

    _write_plan_state(temp_plan_dir, phase="executing")
    _orch, result = _run_cycle(
        temp_plan_dir, temp_project_dir, mock_coding_tool, stopped,
    )

    assert result["status"] == "loop_stopped", result
    assert result["infrastructure_unresolved"] is True, result
    assert result["repair_tasks"] == [], (
        f"没被判过的产品不得派修复任务，实际是 {result!r}"
    )


# ---------------------------------------------------------------------------
# 5. The negative control: a real defect is still repaired
# ---------------------------------------------------------------------------


def test_negative_control_real_defect_still_repaired(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """The negative control: a real defect is still repaired.

    The infra point buys no repair task; the broken deliverable does.
    If the infra terminal swallowed the real defect as well, the
    product would stay broken and nobody would be told -- which is the
    other half of the accident.
    """
    _write_plan_state(temp_plan_dir, phase="executing")

    report = _llm_report()
    report = Orchestrator._backfill_failure_classes(
        report, _accident_execution_results(),
    )
    report = Orchestrator._mark_infrastructure_unresolved(report, ["VP-006"])
    report["failure_class_counts"] = Orchestrator._failure_class_counts(report)

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"), \
         patch(
             "verification.verification_report_reader."
             "extract_failed_vps_with_paths",
         ) as mock_extract, \
         patch("repair_generator.RepairTaskAssembler") as mock_assembler:
        mock_extract.return_value = [
            {
                "id": "VP-006",
                "title": "the point the environment ate",
                "priority": "high",
                "failure_class": "infra",
                "failure_terminal": INFRA_UNRESOLVED_TERMINAL,
            },
            {
                "id": "VP-026",
                "title": "the broken deliverable",
                "priority": "high",
                "failure_class": "deliverable_defect",
            },
        ]
        mock_assembler.return_value.assemble.return_value = [
            {
                "id": "RP-1",
                "title": "the broken deliverable",
                "failed_vp_id": "VP-026",
            },
        ]
        orch = VerificationOrchestrator(
            temp_plan_dir, temp_project_dir, mock_coding_tool,
        )
        orch.repair_generator.generate_repair_contents.return_value = [
            {"failed_vp_id": "VP-026", "title": "the broken deliverable"},
        ]
        orch.start_verification_cycle(round_number=1)
        result = orch.check_cycle_conditions(report)

    repaired = {
        str(task.get("failed_vp_id") or "") for task in result["repair_tasks"]
    }
    assert "VP-026" in repaired, (
        f"真缺陷必须进修复任务，实际修复了 {sorted(repaired)}"
    )
    assert "VP-006" not in repaired, (
        f"基础设施未解决的点不得进修复任务，实际修复了 {sorted(repaired)}"
    )

    offered = orch.repair_generator.generate_repair_contents.call_args
    offered_ids = {
        str(vp.get("id") or "") for vp in (offered.args[0] if offered else [])
    }
    assert "VP-006" not in offered_ids, (
        f"基础设施未解决的点不得出现在修复模型面前，实际是 {sorted(offered_ids)}"
    )
    assert "VP-026" in offered_ids, (
        f"真缺陷必须出现在修复模型面前，实际是 {sorted(offered_ids)}"
    )


# ---------------------------------------------------------------------------
# 6. The negative control: a mislabel is corrected in both directions
# ---------------------------------------------------------------------------


def test_negative_control_llm_mislabels_are_corrected():
    """The negative control: the executor's verdict overrules the report.

    Both directions matter.  An ``infra`` the report called a defect
    buys no repair task -- that is the accident.  A defect the report
    called ``infra`` would be retried forever while the product stays
    broken -- that is the accident's twin.
    """
    execution_results = {
        "execution_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                "failure_class": "infra",
                "failure_class_source": "deterministic",
                "actual_result": CODING_TOOL_DEATH,
            },
            {
                "id": "VP-026",
                "status": "FAILED",
                "failure_class": "deliverable_defect",
                "failure_class_source": "defaulted",
                "actual_result": "断言未通过",
            },
        ],
    }
    report = {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": "VP-006",
                "status": "FAILED",
                # the report called the environment's failure a defect
                "failure_class": "deliverable_defect",
                "failure_class_source": "defaulted",
            },
            {
                "id": "VP-026",
                "status": "FAILED",
                # and the report called the real defect an infra failure
                "failure_class": "infra",
                "failure_class_source": "deterministic",
            },
        ],
    }

    report = Orchestrator._backfill_failure_classes(report, execution_results)

    first, second = report["verification_results"]
    assert first["failure_class"] == "infra", first
    assert first["failure_class_source"] == "deterministic", first
    assert second["failure_class"] == "deliverable_defect", second
    assert second["failure_class_source"] == "defaulted", second


# ---------------------------------------------------------------------------
# 7. The report on disk keeps the classes
# ---------------------------------------------------------------------------


def test_report_written_to_disk_keeps_classes(temp_plan_dir):
    """The classes survive the write to disk.

    ``verification_report.json`` is the only source of truth the
    downstream readers take their numbers from.  A class that is lost
    on the way to disk is the accident, one layer further down.
    """
    report = _llm_report()
    report = Orchestrator._backfill_failure_classes(
        report, _accident_execution_results(),
    )
    report["failure_class_counts"] = Orchestrator._failure_class_counts(report)

    VerificationPersistenceManager(temp_plan_dir).update_report(report)

    on_disk = json.loads(
        (temp_plan_dir / "verification_report.json").read_text(encoding="utf-8")
    )

    entries = {entry["id"]: entry for entry in on_disk["verification_results"]}
    assert entries["VP-006"]["failure_class"] == "infra", on_disk
    assert entries["VP-006"]["failure_class_source"] == "deterministic", on_disk
    assert entries["VP-026"]["failure_class"] == "deliverable_defect", on_disk
    assert entries["VP-026"]["failure_class_source"] == "defaulted", on_disk
    assert on_disk["failure_class_counts"] == {
        "infra": 1,
        "deliverable_defect": 1,
        "criterion_defect": 0,
        "out_of_scope": 0,
    }, on_disk.get("failure_class_counts")
