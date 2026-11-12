"""The e2e leg's failure fingerprint — the identity of the failing set.

2026-11-06 (VP-015, root-cause round 1).

VP-015 failed twice with two different fingerprints
(``e38fbf818e94c8ed`` → ``0c75226e955636eb``) and the same rule
(``legacy-v2``).  Nothing about the defect had changed: the browser leg
had never written a machine-readable failing set, so the fingerprint
fell back to hashing the **first line of the LLM's prose** — and prose
changes when the wording changes.  Two rounds of "the root cause
evolved" for one defect.

The rule selection already had the right answer — "a failing set means
the identity rule" — what was missing was the **supply**: for a browser
leg there was no failing set to hand it.  This file pins that supply:

  * the checkpoints the attempt wrote are read as the failing set;
  * with a failing set, the fingerprint is the identity of that set and
    the run's prose does not enter it;
  * without one, the legacy text-based rule still applies — the old
    path is not quietly rewritten.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification.failure_fingerprint import (  # noqa: E402
    FINGERPRINT_RULE_IDENTITY,
    FINGERPRINT_RULE_LEGACY,
    failure_fingerprint,
    fingerprint_rule,
)
from verification.orchestrator import VerificationOrchestrator  # noqa: E402
from verification_agent import VerificationAgent  # noqa: E402
from verification_subagent import VerificationSubAgent  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _checkpoints(*passed: bool) -> List[Dict[str, Any]]:
    return [
        {
            "selector": f"#check-{index}",
            "expected": "green",
            "actual": "green" if ok else "red",
            "passed": ok,
        }
        for index, ok in enumerate(passed)
    ]


def _write_checkpoints(directory: Path, checkpoints: List[Dict[str, Any]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "checkpoints.json").write_text(
        json.dumps({"checkpoints": checkpoints}, ensure_ascii=False),
        encoding="utf-8",
    )


class _StubTool:
    """The orchestrator's constructor only stores the tool it is given."""

    def __init__(self) -> None:
        self.calls = 0

    def query_json(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self.calls += 1
        return {"verdict": "PASSED", "reasons": [], "evidence": []}


# ---------------------------------------------------------------------------
# 1. the rule follows the presence of a machine-written failing set
# ---------------------------------------------------------------------------


def test_a_machine_written_failing_set_uses_the_identity_rule():
    """With a failing set, the fingerprint is the identity of the defect."""
    assert fingerprint_rule(failed_cases=["checkpoint[1]"]) == FINGERPRINT_RULE_IDENTITY
    assert fingerprint_rule(failed_cases=[]) == FINGERPRINT_RULE_LEGACY
    assert fingerprint_rule(failed_cases=None) == FINGERPRINT_RULE_LEGACY


# ---------------------------------------------------------------------------
# 2. the same failing set fingerprints the same, whatever the prose says
# ---------------------------------------------------------------------------


def test_the_same_failing_set_fingerprints_the_same_whatever_the_prose_says():
    """This is the property the two VP-015 rounds did not have.

    The two fingerprints recorded for one defect differed only because
    the LLM wrote the failure down differently.  With the failing set
    supplied, the prose is out of the hash.
    """
    first = failure_fingerprint(
        vp_id="VP-015",
        failure_class="deliverable_defect",
        first_error_line=(
            "stuck sub-agent: no test output captured at "
            "'/tmp/vp_VP-015_progress.log'; cannot summarize"
        ),
        failed_cases=["checkpoint[1]"],
    )
    second = failure_fingerprint(
        vp_id="VP-015",
        failure_class="deliverable_defect",
        first_error_line=(
            "the browser leg wrote its checkpoints; one of them did not pass"
        ),
        failed_cases=["checkpoint[1]"],
    )

    assert first == second


def test_a_different_failing_set_is_a_different_defect():
    """The identity rule still tells two defects apart."""
    first = failure_fingerprint(
        vp_id="VP-015",
        failure_class="deliverable_defect",
        failed_cases=["checkpoint[1]"],
    )
    second = failure_fingerprint(
        vp_id="VP-015",
        failure_class="deliverable_defect",
        failed_cases=["checkpoint[2]"],
    )

    assert first != second


# ---------------------------------------------------------------------------
# 3. the supply — the orchestrator reads the failing set out of the artifact
# ---------------------------------------------------------------------------


def test_the_orchestrator_reads_the_failed_checkpoints_out_of_the_artifact(
    tmp_path,
):
    """A browser leg's failing checkpoints become the failing set.

    The report entry carried no ``failed_assertions`` for a browser leg
    (there was no machine-written set to carry), so the fingerprint fell
    back to the prose.  The checkpoints the attempt wrote are that set.
    """
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    plan_dir.mkdir(parents=True)
    _write_checkpoints(
        plan_dir / "vp_artifacts" / "VP-015",
        _checkpoints(True, False, True),
    )

    orch = VerificationOrchestrator(plan_dir, project_dir, _StubTool())

    assert orch._failed_case_ids("VP-015", {"id": "VP-015"}) == ["checkpoint[1]"]


def test_the_orchestrator_reads_the_machine_written_failure_record(tmp_path):
    """``failure.json`` is the other machine-written record of the set."""
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    plan_dir.mkdir(parents=True)
    directory = plan_dir / "vp_artifacts" / "VP-015"
    directory.mkdir(parents=True)
    (directory / "failure.json").write_text(
        json.dumps({
            "vp_id": "VP-015",
            "reason_code": "stuck_sub_agent",
            "failure_class": "infra",
            "failed_cases": ["checkpoint[3]"],
        }),
        encoding="utf-8",
    )

    orch = VerificationOrchestrator(plan_dir, project_dir, _StubTool())

    assert orch._failed_case_ids("VP-015", {"id": "VP-015"}) == ["checkpoint[3]"]


def test_without_an_artifact_there_is_no_failing_set(tmp_path):
    """No artifact, no set — the legacy path stays in force."""
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    plan_dir.mkdir(parents=True)

    orch = VerificationOrchestrator(plan_dir, project_dir, _StubTool())

    assert orch._failed_case_ids("VP-015", {"id": "VP-015"}) == []


# ---------------------------------------------------------------------------
# 4. the round's own bookkeeping — what the tracking file records
# ---------------------------------------------------------------------------
#
# 2026-11-06（VP-015 根因 · 层六）：层六的验收判据不是"指纹算对了"，而是
# 下一轮读到的那个**文件**里写着 ``identity-v1`` ——
# ``verification_loop_tracking.json`` 的 ``failure_fingerprint_rules``。
# 下面两条把这个文件本身钉住。


def _report_entry(reasons: str, actual_result: str) -> Dict[str, Any]:
    """A failed report entry, worded the way a run would word it."""
    return {
        "id": "VP-015",
        "status": "FAILED",
        "reasons": [reasons],
        "actual_result": actual_result,
        "evidence": "",
    }


def _orchestrator(tmp_path):
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    plan_dir.mkdir(parents=True)
    return plan_dir, VerificationOrchestrator(
        plan_dir, project_dir, _StubTool(),
    )


def test_the_tracking_file_records_the_identity_rule_for_a_browser_leg(
    tmp_path,
):
    """A browser leg's failure is recorded under the identity rule.

    ``verification_loop_tracking.json`` is what the next round reads to
    tell "the same defect again" from "a different defect under the same
    name".  With the machine-written failing set supplied, the rule it
    records is ``identity-v1`` — not the legacy text rule.
    """
    plan_dir, orch = _orchestrator(tmp_path)
    _write_checkpoints(
        plan_dir / "vp_artifacts" / "VP-015",
        _checkpoints(True, False),
    )

    orch._record_round_fingerprints(
        {"verification_results": [
            _report_entry("checkpoint[1] did not pass", "one checkpoint failed"),
        ]},
        set(),
    )
    orch._save_loop_tracking()

    payload = json.loads(
        (plan_dir / "verification_loop_tracking.json").read_text(
            encoding="utf-8",
        ),
    )
    assert payload["failure_fingerprint_rules"]["VP-015"] == [
        None, "identity-v1",
    ], payload


def test_two_wordings_of_the_same_defect_fingerprint_the_same(tmp_path):
    """The property the two VP-015 rounds did not have.

    The same failing set, reported in two different sentences, is one
    defect — so it must be one fingerprint.  Before the machine-written
    set was supplied, the prose was the only input there was, and
    rewording the failure was enough to make the framework declare the
    root cause had evolved.
    """
    plan_dir, orch = _orchestrator(tmp_path)
    _write_checkpoints(
        plan_dir / "vp_artifacts" / "VP-015",
        _checkpoints(True, False),
    )

    first = orch._round_fingerprint_records({"verification_results": [
        _report_entry(
            "stuck sub-agent: no test output captured",
            "the scenario produced no results",
        ),
    ]})["VP-015"]
    second = orch._round_fingerprint_records({"verification_results": [
        _report_entry(
            "the browser leg wrote its checkpoints; one of them did not pass",
            "checkpoint[1] did not pass",
        ),
    ]})["VP-015"]

    assert first["rule"] == second["rule"] == "identity-v1"
    assert first["fingerprint"] == second["fingerprint"], (
        "the same failing set, worded differently, must be one defect"
    )


def test_without_the_artifact_the_tracking_file_keeps_the_legacy_rule(tmp_path):
    """No machine-written set → the legacy rule, unchanged.

    The old path is not quietly rewritten: a run that produced no
    machine-readable failing set still fingerprints on the prose, and
    the tracking file says so.
    """
    plan_dir, orch = _orchestrator(tmp_path)

    orch._record_round_fingerprints(
        {"verification_results": [
            _report_entry("no test output captured", "the scenario failed"),
        ]},
        set(),
    )
    orch._save_loop_tracking()

    payload = json.loads(
        (plan_dir / "verification_loop_tracking.json").read_text(
            encoding="utf-8",
        ),
    )
    assert payload["failure_fingerprint_rules"]["VP-015"] == [
        None, "legacy-v2",
    ], payload


# ---------------------------------------------------------------------------
# 5. the whole chain — the file the next round reads is written by the
#    framework itself
# ---------------------------------------------------------------------------
#
# 2026-11-06（VP-015 根因 · 验收第 4 条）：第 4 条问的不是"指纹函数算对
# 了吗"，而是**下一轮读到的那个文件里写着什么**。上面两条用的是手写的
# 报告条目；下面这条让框架自己走一遍整条链：浏览器腿留下产物 → 框架从
# 产物重建判决 → 写下 failure.json → 报告条目带上分类与失败集合 →
# 编排器把这一轮记进 tracking 文件。


class _UnusableTool:
    """A tool the artifact path must never reach for."""

    def __init__(self) -> None:
        self.calls = 0

    def query_json(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self.calls += 1
        raise AssertionError("the artifact already answers the question")


@pytest.mark.asyncio
async def test_the_frameworks_own_output_reaches_the_next_round_as_identity(
    tmp_path,
):
    """产物 → 判决 → 报告条目 → tracking 文件，一条链走完。

    这一条是验收第 4 条第 3、4 点的**产物级**证据：分类一路走到报告条目
    仍然是 ``deterministic``（不是 ``defaulted``），而下一轮读的那个
    文件里写着 ``identity-v1``。
    """
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    plan_dir.mkdir(parents=True)
    artifact_dir = plan_dir / "vp_artifacts" / "VP-015"
    _write_checkpoints(artifact_dir, _checkpoints(True, False, True))

    tool = _UnusableTool()
    sub = VerificationSubAgent(method="e2e", max_retries=0)

    verdict = await sub._summarize_vp_result(
        vp_node={"id": "VP-015", "title": "端到端",
                 "expected_result": "green"},
        output_file=None,
        coding_tool=tool,
        log_path=tmp_path / "vp_attempt.log",
        artifact_dir=artifact_dir,
    )

    assert verdict.verdict == "FAILED"
    assert tool.calls == 0, "the artifact already answers the question"
    assert verdict.failure_class_source != "defaulted"

    entry = VerificationAgent._verdict_to_result("VP-015", verdict)
    assert entry["failure_class_source"] != "defaulted"

    agent = VerificationAgent.__new__(VerificationAgent)
    agent.plan_dir = plan_dir
    agent.project_dir = project_dir
    agent._attach_machine_written_failed_cases("VP-015", entry)
    assert entry["failed_assertions"] == ["checkpoint[1]"]

    orch = VerificationOrchestrator(plan_dir, project_dir, _StubTool())
    orch._record_round_fingerprints(
        {"verification_results": [entry]},
        set(),
    )
    orch._save_loop_tracking()

    payload = json.loads(
        (plan_dir / "verification_loop_tracking.json").read_text(
            encoding="utf-8",
        ),
    )
    assert payload["failure_fingerprint_rules"]["VP-015"] == [
        None, "identity-v1",
    ], payload

