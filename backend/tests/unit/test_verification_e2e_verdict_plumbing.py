"""The e2e leg's verdict plumbing — budget, late artifacts, classification.

2026-11-06 (VP-015, root-cause round 1).

VP-015 declares ``timeout_seconds: 3600``, but the e2e method's
wall-clock budget was pinned to 900 seconds, so the attempt was cut off
at 900.042 s — a hundred seconds *before* the browser leg had written
its ``checkpoints.json``.  The framework then went looking for a pytest
tee log (a browser leg never writes one), synthesised a FAILED with no
failure class, and that unclassified verdict was filed as
``deliverable_defect`` / ``defaulted`` — which is how a framework
timeout became a product repair task.

This file pins the five layers that fix it:

  * **L1** the budget honours the VP's declared ``timeout_seconds``;
  * **L2** a stuck attempt is recovered from the artifact it actually
    wrote (``checkpoints.json``), not from a pytest log;
  * **L3** a verdict the framework made up carries a classification;
  * **L4** a late artifact is waited for, and an attempt that is
    terminated says so;
  * **L5** the e2e method reaches the evidence check its contract
    already promised.

The fingerprint side (L6) lives in
``tests/unit/verification/test_e2e_failure_fingerprint_identity.py``.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_subagent as vs  # noqa: E402
from verification_subagent import (  # noqa: E402
    DETERMINISTIC_FAILURE_CLASS_SOURCE,
    INFRA_FAILURE_CLASS,
    VerificationSubAgent,
)
from verification_agent import VerificationAgent  # noqa: E402
from verification_evidence import EVIDENCE_REQUIRED_METHODS  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _agent(method: str = "e2e") -> VerificationSubAgent:
    return VerificationSubAgent(method=method, max_retries=0)


def _artifact_dir(tmp_path: Path, vp_id: str = "VP-015") -> Path:
    directory = tmp_path / "vp_artifacts" / vp_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _checkpoint(passed: bool, selector: str = "#status") -> Dict[str, Any]:
    return {
        "selector": selector,
        "expected": "green",
        "actual": "green" if passed else "red",
        "passed": passed,
    }


def _write_checkpoints(
    directory: Path, checkpoints: List[Dict[str, Any]],
) -> Path:
    path = directory / "checkpoints.json"
    path.write_text(
        json.dumps({"checkpoints": checkpoints}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


class _NeverCalledTool:
    """A coding-tool double that must NOT be asked for a verdict.

    The artifact already answers the question, so a summarizer call
    would be the framework asking a model to guess what the artifact
    says out loud.
    """

    def __init__(self) -> None:
        self.calls = 0

    def query_json(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self.calls += 1
        raise AssertionError("the summarizer must not be called")


class _RaisingTool:
    def __init__(self) -> None:
        self.calls = 0

    def query_json(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self.calls += 1
        raise RuntimeError("provider is unreachable")


class _GarbageTool:
    def __init__(self) -> None:
        self.calls = 0

    def query_json(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self.calls += 1
        return {"not": "a verdict"}


async def _summarize(
    agent: VerificationSubAgent,
    tmp_path: Path,
    *,
    output_file: Optional[Path] = None,
    tool: Any = None,
    artifact_dir: Optional[Path] = None,
    terminated: bool = False,
):
    return await agent._summarize_vp_result(
        vp_node={
            "id": "VP-015",
            "title": "端到端",
            "expected_result": "green",
        },
        output_file=str(output_file) if output_file else None,
        coding_tool=tool if tool is not None else _NeverCalledTool(),
        log_path=tmp_path / "vp_attempt.log",
        artifact_dir=artifact_dir,
        terminated=terminated,
    )


# ---------------------------------------------------------------------------
# L1 — the budget honours the VP's declared timeout_seconds
# ---------------------------------------------------------------------------


def test_a_declared_timeout_raises_the_budget():
    """VP-015 declares 3600 — so 3600 is what the attempt gets."""
    assert _agent().wall_clock_cap({"timeout_seconds": 3600}) == 3600


def test_without_a_declared_timeout_the_method_floor_stays():
    """A VP that declares nothing keeps the method's 900 seconds."""
    assert _agent().wall_clock_cap({}) == 900
    assert _agent().wall_clock_cap(None) == 900
    assert _agent("code_review").wall_clock_cap({"timeout_seconds": 3600}) == 3600


def test_a_declared_timeout_cannot_shrink_the_method_floor():
    """A legacy plan's ``timeout_seconds: 120`` may not squeeze e2e.

    The floor exists because a browser leg that hangs quietly must not
    be able to buy itself an unbounded round by declaring a small
    timeout — and a *small* declared value must not turn the 900-second
    floor into 120 either.
    """
    assert _agent().wall_clock_cap({"timeout_seconds": 120}) == 900


# ---------------------------------------------------------------------------
# L2 — a stuck attempt is recovered from the artifact it wrote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stuck_attempt_with_passing_checkpoints_passes(tmp_path):
    """The artifact says every checkpoint passed — that is a PASS."""
    artifact_dir = _artifact_dir(tmp_path)
    _write_checkpoints(artifact_dir, [_checkpoint(True), _checkpoint(True)])
    tool = _NeverCalledTool()

    verdict = await _summarize(
        _agent(), tmp_path, tool=tool, artifact_dir=artifact_dir,
    )

    assert verdict.verdict == "PASSED"
    assert tool.calls == 0, "the artifact already answers the question"
    assert any("checkpoints.json" in reason for reason in verdict.reasons)
    assert not any(
        "no test output captured" in reason for reason in verdict.reasons
    ), (
        "the attempt DID produce a result; saying it produced none is the "
        "false statement this layer exists to remove"
    )


@pytest.mark.asyncio
async def test_a_stuck_attempt_with_failing_checkpoints_fails_by_name(tmp_path):
    """A failed checkpoint is a failure — and it is named, machine-written.

    The failing checkpoints' ids are written to ``failure.json`` — the
    machine-written failure record the fingerprint layer reads — so the
    report, the artifact and the fingerprint all name the same set.
    """
    artifact_dir = _artifact_dir(tmp_path)
    _write_checkpoints(
        artifact_dir, [_checkpoint(True, "#a"), _checkpoint(False, "#b")],
    )

    verdict = await _summarize(
        _agent(), tmp_path, artifact_dir=artifact_dir,
    )

    assert verdict.verdict == "FAILED"

    record = json.loads(
        (artifact_dir / "failure.json").read_text(encoding="utf-8"),
    )
    assert record["failed_cases"] == ["checkpoint[1]"]
    assert record["failure_class"]
    assert record["reason_code"]
    # 2026-11-06：失败记录**不得**再落成 ``defaulted`` —— 那正是"框架
    # 自己没读产物"被归档成产品缺陷（并开出产品修复任务）的那条路。
    assert record["failure_class_source"] == DETERMINISTIC_FAILURE_CLASS_SOURCE
    assert record["failure_class_source"] != "defaulted"


# ---------------------------------------------------------------------------
# L3 — a verdict the framework made up carries a classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_no_output_verdict_is_classified_as_infra(tmp_path):
    """The framework's own "I found nothing" is not a broken deliverable."""
    verdict = await _summarize(_agent(), tmp_path)

    assert verdict.verdict == "FAILED"
    assert verdict.failure_class == INFRA_FAILURE_CLASS
    assert verdict.failure_class_source == DETERMINISTIC_FAILURE_CLASS_SOURCE


@pytest.mark.asyncio
async def test_the_summarizer_query_failure_is_classified_as_infra(tmp_path):
    """The summarizer could not be reached — again the framework's fault."""
    log = tmp_path / "vp_VP-015_progress.log"
    log.write_text("pytest said something\n", encoding="utf-8")

    verdict = await _summarize(
        _agent(), tmp_path, output_file=log, tool=_RaisingTool(),
    )

    assert verdict.verdict == "FAILED"
    assert verdict.failure_class == INFRA_FAILURE_CLASS
    assert verdict.failure_class_source == DETERMINISTIC_FAILURE_CLASS_SOURCE


@pytest.mark.asyncio
async def test_the_unparseable_summarizer_verdict_is_classified_as_infra(tmp_path):
    """The summarizer answered nonsense — again the framework's fault."""
    log = tmp_path / "vp_VP-015_progress.log"
    log.write_text("pytest said something\n", encoding="utf-8")

    verdict = await _summarize(
        _agent(), tmp_path, output_file=log, tool=_GarbageTool(),
    )

    assert verdict.verdict == "FAILED"
    assert verdict.failure_class == INFRA_FAILURE_CLASS
    assert verdict.failure_class_source == DETERMINISTIC_FAILURE_CLASS_SOURCE


def test_the_framework_made_verdict_keeps_its_class_in_the_report_entry(
    tmp_path,
):
    """``defaulted`` must not survive into the report entry.

    2026-11-06（VP-015 根因 · 层三 / 验收第 4 条第 3 点）：一个**没有**
    分类的判决会落成 ``deliverable_defect`` / ``defaulted``，于是"框架
    超时"被归档成产品缺陷并开出产品修复任务。分类必须一路走到报告条目
    那一层 —— 条目是修复任务与失败指纹读的东西。
    """
    from verification_subagent import Verdict

    verdict = Verdict(
        verdict="FAILED",
        reasons=["stuck sub-agent: no test output captured"],
        evidence=[],
        failure_class=INFRA_FAILURE_CLASS,
        failure_class_source=DETERMINISTIC_FAILURE_CLASS_SOURCE,
    )

    entry = VerificationAgent._verdict_to_result("VP-015", verdict)

    assert entry["failure_class"] == INFRA_FAILURE_CLASS
    assert entry["failure_class_source"] == DETERMINISTIC_FAILURE_CLASS_SOURCE
    assert entry["failure_class_source"] != "defaulted"


# ---------------------------------------------------------------------------
# L4 — the grace window, and the termination that follows it
# ---------------------------------------------------------------------------


class _HangingTool:
    """A coding-tool double whose call never returns in time.

    It sleeps long enough for the (tiny) work budget to expire, and
    long enough for the abandoned worker thread to still be alive when
    the framework decides what to do with it.
    """

    def __init__(self, seconds: float = 1.0) -> None:
        self.seconds = seconds
        self.calls = 0

    def query_json(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self.calls += 1
        time.sleep(self.seconds)
        return {"verdict": "PASSED", "reasons": [], "evidence": []}


@pytest.mark.asyncio
async def test_the_grace_window_adopts_an_artifact_that_lands_late(
    tmp_path, monkeypatch,
):
    """The artifact lands after the budget ran out — it is still the answer.

    This is the exact shape of the 2026-11-06 failure: the framework
    gave up at 900.042 s and the browser leg wrote its checkpoints a
    hundred seconds later.
    """
    monkeypatch.setattr(vs, "STUCK_GRACE_SECONDS", 10)
    monkeypatch.setattr(vs, "STUCK_GRACE_POLL_SECONDS", 0.05)

    artifact_dir = _artifact_dir(tmp_path)

    # The attempt's budget is one second; the grace window takes half of
    # it (the rest is the work phase), so the work phase ends at ~0.5 s
    # and the artifact lands at 0.8 s — after the work phase, inside the
    # window.  That is the shape of the 2026-11-06 failure: the verdict
    # deadline came first, the artifact second.
    def _land_the_artifact() -> None:
        time.sleep(0.8)
        _write_checkpoints(artifact_dir, [_checkpoint(True)])

    timer = threading.Thread(target=_land_the_artifact)
    timer.start()
    try:
        agent = _agent()
        agent.HARD_WALL_CLOCK_CAP_SECONDS = 1.0
        verdict = await agent._execute_attempt(
            vp_node={
                "id": "VP-015",
                "title": "端到端",
                "expected_result": "green",
                "timeout_seconds": 3600,
            },
            coding_tool=_HangingTool(seconds=1.5),
            settings_path=str(tmp_path / "settings.json"),
            log_path=tmp_path / "vp_attempt.log",
            attempt=0,
            project_dir=tmp_path,
            artifact_dir=artifact_dir,
        )
    finally:
        timer.join()

    assert verdict.verdict == "PASSED"
    assert any("checkpoints.json" in reason for reason in verdict.reasons)


@pytest.mark.asyncio
async def test_a_terminated_attempt_says_it_was_terminated(tmp_path):
    """No artifact and no output: the reason names the termination.

    "The attempt was terminated and produced nothing" is a statement
    about the framework's decision; "the scenario produced no results" is
    a statement about the product, and it is false whenever the product
    wrote something the framework never read.
    """
    artifact_dir = _artifact_dir(tmp_path)

    verdict = await _summarize(
        _agent(), tmp_path, artifact_dir=artifact_dir, terminated=True,
    )

    assert verdict.verdict == "FAILED"
    assert any("terminated" in reason for reason in verdict.reasons)
    assert not any(
        "no test output captured" in reason for reason in verdict.reasons
    )


class _ProductionShapedTool:
    """A coding-tool double shaped like the production one.

    Production shape: when the outer cap fires, the branch ends the
    in-flight dispatch FIRST — and ending it **drops** the process
    handle — and only THEN asks ``_terminate_attempt_process`` to
    terminate the attempt.  The follow-up therefore finds no process
    left to kill and reports "nothing was terminated", even though the
    framework is the one that ended the attempt.
    """

    def __init__(self, seconds: float = 2.0) -> None:
        self.seconds = seconds
        self.calls = 0
        self.dispatch_kills = 0
        self._process_lock = threading.Lock()
        self._current_process = object()

    def kill_in_flight_dispatch(self, reason: str = "") -> bool:
        # What the real tool does: end the dispatch, drop the handle.
        self.dispatch_kills += 1
        with self._process_lock:
            self._current_process = None
        return True

    def query_json(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self.calls += 1
        time.sleep(self.seconds)
        return {"verdict": "PASSED", "reasons": [], "evidence": []}


@pytest.mark.asyncio
async def test_the_outer_cap_branch_says_the_attempt_was_terminated(
    tmp_path,
):
    """The production branch has to work out "terminated" for itself.

    The test above injects ``terminated=True`` straight into the
    summarizer, so it never touches the branch that *decides* it — and
    that branch was wrong.  The outer cap ends the in-flight dispatch
    first, which drops the process handle, so the termination that
    follows finds nothing to kill and reports "not terminated".  The
    verdict then degraded into "no test output captured" — a claim
    about the deliverable that the framework itself made untrue,
    because the framework is the one that ended the attempt.

    The vp_node carries the **production** ``evidence_artifact_dir``
    (``<plan_dir>/vp_artifacts/<vp_id>`` — the value the dispatcher
    writes for every VP it runs).  Without it the browser leg's
    artifact branch is skipped, and this pin exercises the pytest
    leg's log fallback instead — the branch production never takes
    for a browser verification point (2026-11-10, VP-032).
    """
    artifact_dir = _artifact_dir(tmp_path)
    log_path = tmp_path / "vp_attempt.log"

    agent = _agent()
    agent.HARD_WALL_CLOCK_CAP_SECONDS = 1.0
    tool = _ProductionShapedTool(seconds=2.0)

    verdict = await agent._self_heal_loop(
        vp_node={
            "id": "VP-015",
            "title": "端到端",
            "expected_result": "green",
            "verification_method": "e2e",
            "evidence_artifact_dir": str(artifact_dir),
            "timeout_seconds": 3600,
        },
        coding_tool=tool,
        settings_path=str(tmp_path / "settings.json"),
        log_path=log_path,
        project_dir=tmp_path,
        artifact_dir=artifact_dir,
    )

    assert tool.dispatch_kills == 1, (
        "the outer cap did not end the in-flight dispatch — the test "
        "did not exercise the production branch"
    )
    assert verdict.verdict == "FAILED"
    assert any("terminated" in reason for reason in verdict.reasons), (
        f"the verdict does not say the attempt was terminated: "
        f"{verdict.reasons!r}"
    )
    assert not any(
        "no test output captured" in reason for reason in verdict.reasons
    ), (
        f"the verdict blames the deliverable for a termination the "
        f"framework caused: {verdict.reasons!r}"
    )

    # The artifact statement is still there: naming the artifact is a
    # fact about the walk, and the termination disclosure does not
    # replace it.
    assert any(
        "evidence artifact" in reason for reason in verdict.reasons
    ), (
        f"the verdict does not name the artifact it could not read: "
        f"{verdict.reasons!r}"
    )
    assert any(
        str(artifact_dir) in reason for reason in verdict.reasons
    ), (
        f"the verdict does not name the artifact directory: "
        f"{verdict.reasons!r}"
    )

    # A framework-made verdict stays classified as the framework's.
    assert verdict.failure_class == INFRA_FAILURE_CLASS, (
        f"the verdict lost its failure class: {verdict!r}"
    )
    assert verdict.failure_class_source == DETERMINISTIC_FAILURE_CLASS_SOURCE

    # The event and the verdict must tell the same story.
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    terminated_events = [
        entry for entry in events
        if entry.get("event") == "stuck_attempt_terminated"
    ]
    assert terminated_events, (
        "the attempt was ended but nothing logged that it was"
    )
    assert terminated_events[-1]["data"]["terminated"] is True, (
        f"the log and the verdict disagree about the termination: "
        f"{terminated_events[-1]['data']!r}"
    )


@pytest.mark.asyncio
async def test_the_artifact_branch_discloses_the_termination(tmp_path):
    """The artifact branch must not hide that the framework ended it.

    2026-11-10 (VP-032): the browser leg's artifact branch returns
    BEFORE the branch that discloses the termination, so a verdict
    built from a missing artifact said only "the walk cannot be
    graded" — a statement about the deliverable, made untrue by the
    framework's own decision to stop the walk.  In production every
    dispatched VP carries ``evidence_artifact_dir``, so this branch is
    the one every browser verification point takes.
    """
    artifact_dir = _artifact_dir(tmp_path)

    verdict = await _agent()._summarize_vp_result(
        vp_node={
            "id": "VP-015",
            "title": "端到端",
            "expected_result": "green",
            "verification_method": "e2e",
            "evidence_artifact_dir": str(artifact_dir),
        },
        output_file=None,
        coding_tool=_NeverCalledTool(),
        log_path=tmp_path / "vp_attempt.log",
        artifact_dir=artifact_dir,
        terminated=True,
    )

    assert verdict.verdict == "FAILED"
    assert any(
        "terminated" in reason.lower() for reason in verdict.reasons
    ), (
        f"the verdict does not disclose that the framework terminated "
        f"the attempt: {verdict.reasons!r}"
    )
    assert not any(
        "no test output captured" in reason for reason in verdict.reasons
    ), (
        f"the verdict blames the deliverable for a termination the "
        f"framework caused: {verdict.reasons!r}"
    )
    assert any(
        "evidence artifact" in reason for reason in verdict.reasons
    ), (
        f"the verdict does not name the artifact it could not read: "
        f"{verdict.reasons!r}"
    )
    assert verdict.failure_class == INFRA_FAILURE_CLASS
    assert verdict.failure_class_source == DETERMINISTIC_FAILURE_CLASS_SOURCE


@pytest.mark.asyncio
async def test_a_missing_artifact_verdict_does_not_claim_a_termination(
    tmp_path,
):
    """The disclosure is earned — without a termination there is none.

    The reverse of the pin above.  A verdict that is FAILED because the
    evidence artifact is missing must NOT say the attempt was
    terminated when nothing terminated it: a disclosure that appeared on
    every missing-artifact verdict would put a false statement about
    the framework's own actions into the report.
    """
    artifact_dir = _artifact_dir(tmp_path)

    verdict = await _agent()._summarize_vp_result(
        vp_node={
            "id": "VP-015",
            "title": "端到端",
            "expected_result": "green",
            "verification_method": "e2e",
            "evidence_artifact_dir": str(artifact_dir),
        },
        output_file=None,
        coding_tool=_NeverCalledTool(),
        log_path=tmp_path / "vp_attempt.log",
        artifact_dir=artifact_dir,
        terminated=False,
    )

    assert verdict.verdict == "FAILED"
    assert not any(
        "terminated" in reason.lower() for reason in verdict.reasons
    ), (
        f"the verdict claims a termination that did not happen: "
        f"{verdict.reasons!r}"
    )
    # The fact the verdict does have is still told: the artifact it
    # could not read.
    assert any(
        "evidence artifact" in reason for reason in verdict.reasons
    ), (
        f"the verdict does not name the artifact it could not read: "
        f"{verdict.reasons!r}"
    )


@pytest.mark.asyncio
async def test_the_stuck_error_carries_the_termination_the_branch_found(
    tmp_path,
):
    """``StuckAgentError.terminated`` is the branch's own answer.

    The contract is one value in two places — the exception and the
    ``stuck_attempt_terminated`` event — so that the log and the
    verdict cannot disagree about what the framework did.  This pins
    the exception's half of that contract on the **production branch**:
    the timeout branch computes the value itself (the branch that used
    to hand back ``False`` while the attempt was being terminated).
    """
    artifact_dir = _artifact_dir(tmp_path)
    log_path = tmp_path / "vp_attempt.log"

    agent = _agent()
    agent.HARD_WALL_CLOCK_CAP_SECONDS = 1.0
    tool = _ProductionShapedTool(seconds=2.0)

    with pytest.raises(vs.StuckAgentError) as excinfo:
        await agent._execute_attempt(
            vp_node={
                "id": "VP-015",
                "title": "端到端",
                "expected_result": "green",
                "verification_method": "e2e",
                "timeout_seconds": 3600,
            },
            coding_tool=tool,
            settings_path=str(tmp_path / "settings.json"),
            log_path=log_path,
            attempt=0,
            project_dir=tmp_path,
            artifact_dir=artifact_dir,
        )

    assert excinfo.value.terminated is True, (
        "the exception says the attempt was not terminated, although "
        "the framework is the one that ended it"
    )
    assert excinfo.value.killed is True, (
        "the exception does not say the in-flight dispatch was ended"
    )

    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    terminated_events = [
        entry for entry in events
        if entry.get("event") == "stuck_attempt_terminated"
    ]
    assert terminated_events, "the termination was not logged"
    data = terminated_events[-1]["data"]
    assert data["terminated"] is True
    assert data["killed"] is True, (
        f"the log does not say the dispatch was ended: {data!r}"
    )


# ---------------------------------------------------------------------------
# L6 (VP-061) — the termination fact reaches the artifact branch too
# ---------------------------------------------------------------------------
#
# 2026-11-11 (VP-061): the artifact branch decides from the checkpoints
# the attempt wrote, and it decided **silently about the termination**.
# The dispatcher hands every VP it runs an ``evidence_artifact_dir``
# (``<plan_dir>/vp_artifacts/<vp_id>``), so for a browser leg this
# branch — not the one that discloses — is the one production takes.
# A verdict built here therefore said nothing about the framework's own
# decision to stop the attempt, and read like a verdict on the
# deliverable.
#
# The disclosure is asserted as a **prefix**, never as a loose
# ``"terminated" in reason.lower()``: a pytest temporary directory can
# carry any word in its name, and a loose substring assertion passes on
# the directory name alone.

_STOP_DISCLOSURE_PREFIX = (
    "stuck sub-agent: the attempt was terminated after its budget ran out"
)


def _production_shaped_node(artifact_dir: Path) -> Dict[str, Any]:
    """The vp_node the dispatcher actually hands a browser leg.

    ``verification_method`` plus the ``evidence_artifact_dir`` — the
    shape every dispatched VP carries, and the shape in which the
    browser leg's artifact branch is the branch that runs.
    """
    return {
        "id": "VP-015",
        "title": "端到端",
        "expected_result": "green",
        "verification_method": "e2e",
        "evidence_artifact_dir": str(artifact_dir),
    }


@pytest.mark.asyncio
async def test_the_artifact_branch_with_failing_checkpoints_discloses_the_stop(
    tmp_path,
):
    """A half-passed artifact still has to say the framework stopped it.

    The checkpoints the attempt wrote are the verdict's subject, but
    they are not the whole story: the framework ended the attempt, and a
    reader who cannot tell that apart from a verdict on the deliverable
    will send the wrong repair.
    """
    artifact_dir = _artifact_dir(tmp_path)
    _write_checkpoints(
        artifact_dir, [_checkpoint(True, "#a"), _checkpoint(False, "#b")],
    )
    tool = _NeverCalledTool()

    verdict = await _agent()._summarize_vp_result(
        vp_node=_production_shaped_node(artifact_dir),
        output_file=None,
        coding_tool=tool,
        log_path=tmp_path / "vp_attempt.log",
        artifact_dir=artifact_dir,
        terminated=True,
    )

    assert verdict.verdict == "FAILED"
    assert tool.calls == 0, "the artifact already answers the question"
    assert any(
        reason.startswith(_STOP_DISCLOSURE_PREFIX)
        for reason in verdict.reasons
    ), (
        f"the verdict does not disclose that the framework terminated "
        f"the attempt: {verdict.reasons!r}"
    )
    # The disclosure is **added** to the verdict, not swapped for it:
    # the failing checkpoint is still named, and the machine-written
    # failing set is untouched.
    assert any(
        "checkpoint[1]" in reason for reason in verdict.reasons
    ), (
        f"the verdict no longer names the failing checkpoint: "
        f"{verdict.reasons!r}"
    )
    record = json.loads(
        (artifact_dir / "failure.json").read_text(encoding="utf-8"),
    )
    assert record["failed_cases"] == ["checkpoint[1]"]
    assert record["failure_class_source"] == DETERMINISTIC_FAILURE_CLASS_SOURCE


@pytest.mark.asyncio
async def test_the_artifact_branch_without_a_framework_stop_claims_none(
    tmp_path,
):
    """The disclosure is earned — without a termination there is none.

    The reverse of the pin above: the same production shape, the same
    half-passed artifact, and **no** termination.  A disclosure that
    appeared on every artifact verdict would put a false statement about
    the framework's own actions into the report.
    """
    artifact_dir = _artifact_dir(tmp_path)
    _write_checkpoints(
        artifact_dir, [_checkpoint(True, "#a"), _checkpoint(False, "#b")],
    )

    verdict = await _agent()._summarize_vp_result(
        vp_node=_production_shaped_node(artifact_dir),
        output_file=None,
        coding_tool=_NeverCalledTool(),
        log_path=tmp_path / "vp_attempt.log",
        artifact_dir=artifact_dir,
        terminated=False,
    )

    assert verdict.verdict == "FAILED"
    # The negative assertion must not be vacuous: if the artifact path
    # itself carried the word, the check below would pass on the path.
    assert "terminat" not in str(artifact_dir).lower(), (
        f"the artifact path carries the word itself, so the negative "
        f"assertion would prove nothing: {artifact_dir}"
    )
    assert "terminat" not in " ".join(verdict.reasons).lower(), (
        f"the verdict claims a termination that did not happen: "
        f"{verdict.reasons!r}"
    )
    assert not any(
        reason.startswith(_STOP_DISCLOSURE_PREFIX)
        for reason in verdict.reasons
    ), (
        f"the verdict discloses a termination that did not happen: "
        f"{verdict.reasons!r}"
    )
    # The fact the verdict does have is still told.
    assert any(
        "checkpoint[1]" in reason for reason in verdict.reasons
    ), (
        f"the verdict no longer names the failing checkpoint: "
        f"{verdict.reasons!r}"
    )


@pytest.mark.asyncio
async def test_the_outer_cap_branch_discloses_the_stop_when_the_artifact_lands_late(
    tmp_path, monkeypatch,
):
    """The production outer-cap branch has to disclose as well.

    The outer cap ends the in-flight dispatch **first** — that is the
    production shape — and the late artifact is adopted out of the grace
    window afterwards.  That branch returns before the code that
    computes the termination, so the fact was never even computed: the
    framework had ended the attempt and the verdict did not say so.

    ``dispatch_kills == 1`` is what makes this test load-bearing: if
    the framework had not ended the dispatch, there would be nothing to
    disclose and the assertions below would have to fail.
    """
    monkeypatch.setattr(vs, "STUCK_GRACE_POLL_SECONDS", 0.05)

    artifact_dir = _artifact_dir(tmp_path)

    # The attempt's budget is one second and the grace window takes half
    # of it, so the work phase ends at ~0.5 s and the artifact lands at
    # 0.8 s — after the work phase, inside the window.  That is the
    # shape of the 2026-11-06 failure: the verdict deadline came
    # first, the artifact second.
    def _land_the_artifact() -> None:
        time.sleep(0.8)
        _write_checkpoints(
            artifact_dir, [_checkpoint(True, "#a"), _checkpoint(False, "#b")],
        )

    timer = threading.Thread(target=_land_the_artifact)
    timer.start()
    try:
        agent = _agent()
        agent.HARD_WALL_CLOCK_CAP_SECONDS = 1.0
        tool = _ProductionShapedTool(seconds=2.0)

        verdict = await agent._self_heal_loop(
            vp_node={
                "id": "VP-015",
                "title": "端到端",
                "expected_result": "green",
                "verification_method": "e2e",
                "evidence_artifact_dir": str(artifact_dir),
                "timeout_seconds": 3600,
            },
            coding_tool=tool,
            settings_path=str(tmp_path / "settings.json"),
            log_path=tmp_path / "vp_attempt.log",
            project_dir=tmp_path,
            artifact_dir=artifact_dir,
        )
    finally:
        timer.join()

    assert tool.dispatch_kills == 1, (
        "the outer cap did not end the in-flight dispatch — the test "
        "did not exercise the production branch"
    )
    assert verdict.verdict == "FAILED"
    assert any(
        reason.startswith(_STOP_DISCLOSURE_PREFIX)
        for reason in verdict.reasons
    ), (
        f"the verdict does not disclose that the framework terminated "
        f"the attempt: {verdict.reasons!r}"
    )
    assert any(
        "checkpoint[1]" in reason for reason in verdict.reasons
    ), (
        f"the verdict no longer names the failing checkpoint: "
        f"{verdict.reasons!r}"
    )

    # The log and the verdict must tell the same story: the verdict
    # discloses the termination, so the event that records the recovery
    # has to say whether the framework really ended the attempt.
    events = [
        json.loads(line)
        for line in (tmp_path / "vp_attempt.log").read_text(
            encoding="utf-8",
        ).splitlines()
        if line.strip()
    ]
    recovered = [
        entry for entry in events
        if entry.get("event") == "stuck_artifact_recovered"
    ]
    assert recovered, "the recovered artifact was not logged"
    data = recovered[-1]["data"]
    assert data["terminated"] is True, (
        f"the log disagrees with the verdict about the termination: "
        f"{data!r}"
    )
    assert data["killed"] is True, (
        f"the log does not say the dispatch was ended: {data!r}"
    )


@pytest.mark.asyncio
async def test_the_artifact_branch_with_passing_checkpoints_discloses_the_stop(
    tmp_path,
):
    """The PASSED branch discloses as well — a PASS is not an exemption.

    The disclosure is not a mark of shame: it says the framework ended
    the attempt, and the checkpoints it did write are what the verdict
    can speak for.  A verdict that stayed silent about it would say
    "the walk passed" about a walk the framework cut short.
    """
    artifact_dir = _artifact_dir(tmp_path)
    _write_checkpoints(
        artifact_dir, [_checkpoint(True, "#a"), _checkpoint(True, "#b")],
    )

    verdict = await _agent()._summarize_vp_result(
        vp_node=_production_shaped_node(artifact_dir),
        output_file=None,
        coding_tool=_NeverCalledTool(),
        log_path=tmp_path / "vp_attempt.log",
        artifact_dir=artifact_dir,
        terminated=True,
    )

    assert verdict.verdict == "PASSED"
    assert any(
        reason.startswith(_STOP_DISCLOSURE_PREFIX)
        for reason in verdict.reasons
    ), (
        f"the verdict does not disclose that the framework terminated "
        f"the attempt: {verdict.reasons!r}"
    )
    # The disclosure is added, not swapped for the verdict's own fact.
    assert any(
        "all of them passed" in reason for reason in verdict.reasons
    ), (
        f"the verdict no longer states what the checkpoints said: "
        f"{verdict.reasons!r}"
    )


@pytest.mark.asyncio
async def test_the_artifact_branch_with_passing_checkpoints_claims_no_stop(
    tmp_path,
):
    """The reverse of the pin above: no termination, no disclosure.

    The disclosure is earned.  A PASSED verdict built from an attempt
    the framework never ended must not carry a sentence about a
    termination that did not happen.
    """
    artifact_dir = _artifact_dir(tmp_path)
    _write_checkpoints(
        artifact_dir, [_checkpoint(True, "#a"), _checkpoint(True, "#b")],
    )

    verdict = await _agent()._summarize_vp_result(
        vp_node=_production_shaped_node(artifact_dir),
        output_file=None,
        coding_tool=_NeverCalledTool(),
        log_path=tmp_path / "vp_attempt.log",
        artifact_dir=artifact_dir,
        terminated=False,
    )

    assert verdict.verdict == "PASSED"
    assert "terminat" not in str(artifact_dir).lower(), (
        f"the artifact path carries the word itself, so the negative "
        f"assertion would prove nothing: {artifact_dir}"
    )
    assert "terminat" not in " ".join(verdict.reasons).lower(), (
        f"the verdict claims a termination that did not happen: "
        f"{verdict.reasons!r}"
    )
    assert any(
        "all of them passed" in reason for reason in verdict.reasons
    ), (
        f"the verdict no longer states what the checkpoints said: "
        f"{verdict.reasons!r}"
    )


@pytest.mark.asyncio
async def test_the_artifact_branch_log_records_the_termination(tmp_path):
    """The log has to say what the verdict says.

    The verdict on this branch discloses that the framework ended the
    attempt; the event that records the branch has to carry the same
    fact, or an operator reading the log sees a recovery with no
    mention of the termination the verdict is about.
    """
    artifact_dir = _artifact_dir(tmp_path)
    _write_checkpoints(
        artifact_dir, [_checkpoint(True, "#a"), _checkpoint(False, "#b")],
    )
    log_path = tmp_path / "vp_attempt.log"

    verdict = await _agent()._summarize_vp_result(
        vp_node=_production_shaped_node(artifact_dir),
        output_file=None,
        coding_tool=_NeverCalledTool(),
        log_path=log_path,
        artifact_dir=artifact_dir,
        terminated=True,
    )

    assert verdict.verdict == "FAILED"
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    artifact_events = [
        entry for entry in events
        if entry.get("event") == "summarizer_artifact_verdict"
    ]
    assert artifact_events, "the artifact branch was not logged"
    data = artifact_events[-1]["data"]
    assert data["terminated"] is True, (
        f"the log does not say the attempt was terminated: {data!r}"
    )


# ---------------------------------------------------------------------------
# L2 (edge) — a corrupt artifact is "no artifact", not an exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_corrupt_checkpoints_file_is_treated_as_no_artifact(tmp_path):
    """A half-written artifact must not take the round down with it."""
    artifact_dir = _artifact_dir(tmp_path)
    (artifact_dir / "checkpoints.json").write_text(
        "{not json at all", encoding="utf-8",
    )

    verdict = await _summarize(
        _agent(), tmp_path, artifact_dir=artifact_dir,
    )

    assert verdict.verdict == "FAILED"
    assert verdict.failure_class == INFRA_FAILURE_CLASS


# ---------------------------------------------------------------------------
# L5 — the e2e method reaches the evidence check
# ---------------------------------------------------------------------------


def _bare_agent(plan_dir: Path, project_dir: Path) -> VerificationAgent:
    """A VerificationAgent with only what the evidence check touches."""
    agent = VerificationAgent.__new__(VerificationAgent)
    agent.plan_dir = plan_dir
    agent.project_dir = project_dir

    class _Persistence:
        def write_verification_point_log(self, *args: Any, **kwargs: Any):
            return None

    agent.persistence = _Persistence()
    return agent


def test_e2e_is_part_of_the_evidence_contract():
    """The contract already promised e2e a checkable artifact."""
    assert "e2e" in EVIDENCE_REQUIRED_METHODS


def test_an_e2e_pass_without_checkpoints_is_downgraded(tmp_path):
    """A PASSED e2e with no ``checkpoints.json`` cannot stand.

    The evidence check existed for ``code_review`` and
    ``ui_validation``; ``e2e`` was left out of the caller's filter, so
    the contract ``verification_evidence`` already declared was never
    enforced for the browser leg.
    """
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    plan_dir.mkdir(parents=True)
    agent = _bare_agent(plan_dir, project_dir)

    verdict = agent._apply_evidence_check(
        {"id": "VP-015", "verification_method": "e2e"},
        {"id": "VP-015", "status": "PASSED", "reasons": [], "evidence": ""},
    )

    assert verdict["status"] == "FAILED"
    assert "checkpoints.json" in str(verdict.get("actual_result"))


def test_an_e2e_pass_with_checkpoints_stands(tmp_path):
    """The artifact is there and it is well formed — the PASSED stands."""
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    plan_dir.mkdir(parents=True)
    directory = plan_dir / "vp_artifacts" / "VP-015"
    directory.mkdir(parents=True)
    _write_checkpoints(directory, [_checkpoint(True)])
    agent = _bare_agent(plan_dir, project_dir)

    verdict = agent._apply_evidence_check(
        {"id": "VP-015", "verification_method": "e2e"},
        {"id": "VP-015", "status": "PASSED", "reasons": [], "evidence": ""},
    )

    assert verdict["status"] == "PASSED"


def test_the_machine_written_failing_set_reaches_the_result_dict(tmp_path):
    """The machine-written ids need a path of their own to the report.

    The framework writes the failing checkpoints down when it rebuilds a
    verdict (``failure.json``).  Before this, the only route into
    ``failed_assertions`` was ``_failed_case_ids_from_evidence`` — a
    route that reads ids out of the **prose** handed to the LLM, which
    is not the machine-written set.
    """
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    plan_dir.mkdir(parents=True)
    directory = plan_dir / "vp_artifacts" / "VP-015"
    directory.mkdir(parents=True)
    (directory / "failure.json").write_text(
        json.dumps({
            "vp_id": "VP-015",
            "reason_code": "checkpoints_failed",
            "failure_class": "deliverable_defect",
            "failed_cases": ["checkpoint[1]", "checkpoint[3]"],
        }),
        encoding="utf-8",
    )
    agent = _bare_agent(plan_dir, project_dir)
    result: Dict[str, Any] = {"id": "VP-015", "status": "FAILED"}

    agent._attach_machine_written_failed_cases("VP-015", result)

    assert result["failed_assertions"] == ["checkpoint[1]", "checkpoint[3]"]


def test_a_result_that_already_carries_its_own_set_is_not_overwritten(tmp_path):
    """The entry's own set wins — the machine-written one is a fallback."""
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    plan_dir.mkdir(parents=True)
    directory = plan_dir / "vp_artifacts" / "VP-015"
    directory.mkdir(parents=True)
    (directory / "failure.json").write_text(
        json.dumps({"failed_cases": ["checkpoint[9]"]}),
        encoding="utf-8",
    )
    agent = _bare_agent(plan_dir, project_dir)
    result: Dict[str, Any] = {
        "id": "VP-015", "status": "FAILED",
        "failed_assertions": ["tests/test_x.py::test_a"],
    }

    agent._attach_machine_written_failed_cases("VP-015", result)

    assert result["failed_assertions"] == ["tests/test_x.py::test_a"]
