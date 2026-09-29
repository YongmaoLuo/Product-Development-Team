"""Unit tests for the evidence contract (2026-09-18 C6).

``code_review`` and ``ui_validation`` rest on an LLM's word. This module
is what makes that word checkable:

  * a citation must resolve to a file that exists, a line in range, and
    a snippet actually present near that line;
  * a checkpoint must name a selector and record an actual value;
  * a PASSED resting on unresolvable evidence is downgraded to FAILED.

The downgrade is deliberately one-way — only PASSED is touched — because
a reviewer reporting *missing* code cannot always cite a line, and
downgrading a FAILED would be a no-op anyway.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_evidence import (  # noqa: E402
    CITATIONS_FILENAME,
    CHECKPOINTS_FILENAME,
    MAX_EVIDENCE_OUTPUT_CHARS,
    apply_to_verdict,
    artifact_dir_for,
    attach_evidence_command,
    run_evidence_command,
    verify_checkpoints,
    verify_citations,
    verify_evidence,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "engine.rs").write_text(
        "fn main() {\n"
        "    let metric = find_metric(&bars);\n"
        "    assert!(metric.is_some());\n"
        "}\n"
    )
    return tmp_path


def _write(plan_dir: Path, vp_id: str, name: str, payload) -> Path:
    directory = artifact_dir_for(plan_dir, vp_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(json.dumps(payload, ensure_ascii=False))
    return target


# ---------------------------------------------------------------------------
# verify_citations
# ---------------------------------------------------------------------------


def test_a_resolvable_citation_passes(project: Path):
    citations = [{
        "file": "src/engine.rs",
        "line": 2,
        "snippet": "let metric = find_metric(&bars);",
    }]

    assert verify_citations(citations, project) == []


def test_a_multi_line_snippet_matches_on_its_first_line(project: Path):
    citations = [{
        "file": "src/engine.rs",
        "line": 2,
        "snippet": "let metric = find_metric(&bars);\nassert!(metric.is_some());",
    }]

    assert verify_citations(citations, project) == []


def test_whitespace_and_indentation_do_not_matter(project: Path):
    citations = [{
        "file": "src/engine.rs", "line": 2,
        "snippet": "   let    metric  =  find_metric(&bars);   ",
    }]

    assert verify_citations(citations, project) == []


def test_an_off_by_one_citation_is_tolerated(project: Path):
    citations = [{
        "file": "src/engine.rs", "line": 4,
        "snippet": "let metric = find_metric(&bars);",
    }]

    assert verify_citations(citations, project) == []


@pytest.mark.parametrize(
    "citation, kind",
    [
        ({"line": 1, "snippet": "x"}, "malformed"),
        ({"file": "src/engine.rs", "snippet": "x"}, "malformed"),
        ({"file": "src/engine.rs", "line": 1}, "malformed"),
        ({"file": "src/engine.rs", "line": 1, "snippet": "   "}, "malformed"),
        ({"file": "src/nope.rs", "line": 1, "snippet": "x"}, "missing_file"),
        ({"file": "src/engine.rs", "line": 999, "snippet": "x"}, "line_out_of_range"),
        ({"file": "../outside.rs", "line": 1, "snippet": "x"}, "outside_project"),
        ("not-an-object", "malformed"),
    ],
)
def test_bad_citations_are_reported_with_a_kind(project: Path, citation, kind):
    issues = verify_citations([citation], project)

    assert issues, citation
    assert issues[0].kind == kind


def test_a_snippet_that_is_not_in_the_file_is_rejected(project: Path):
    citations = [{
        "file": "src/engine.rs", "line": 2,
        "snippet": "let this_line_does_not_exist = true;",
    }]

    issues = verify_citations(citations, project)

    assert [i.kind for i in issues] == ["snippet_mismatch"]


def test_an_empty_citation_list_is_not_evidence(project: Path):
    assert [i.kind for i in verify_citations([], project)] == ["missing_evidence"]
    assert [i.kind for i in verify_citations(None, project)] == ["missing_evidence"]


def test_every_citation_is_checked(project: Path):
    citations = [
        {"file": "src/engine.rs", "line": 2, "snippet": "let metric = find_metric(&bars);"},
        {"file": "src/engine.rs", "line": 999, "snippet": "x"},
    ]

    issues = verify_citations(citations, project)

    assert [i.kind for i in issues] == ["line_out_of_range"]


# ---------------------------------------------------------------------------
# verify_checkpoints
# ---------------------------------------------------------------------------


def test_a_well_formed_checkpoint_list_passes():
    checkpoints = [{
        "selector": '[data-testid="chart-grid"]',
        "expected": "chart grid visible",
        "actual": "visible, 3 canvases",
        "passed": True,
    }]

    assert verify_checkpoints(checkpoints) == []


@pytest.mark.parametrize(
    "checkpoint",
    [
        {"expected": "x", "actual": "y", "passed": True},
        {"selector": "s", "actual": "y", "passed": True},
        {"selector": "s", "expected": "x", "passed": True},
        {"selector": "s", "expected": "x", "actual": "y", "passed": "yes"},
        "not-an-object",
    ],
)
def test_bad_checkpoints_are_rejected(checkpoint):
    issues = verify_checkpoints([checkpoint])

    assert issues
    assert all(i.kind == "malformed" for i in issues)


def test_an_empty_checkpoint_list_is_not_evidence():
    assert [i.kind for i in verify_checkpoints([])] == ["missing_evidence"]


# ---------------------------------------------------------------------------
# verify_evidence / apply_to_verdict
# ---------------------------------------------------------------------------


def test_api_test_has_no_artifact_contract(project: Path, tmp_path: Path):
    assert verify_evidence("api_test", tmp_path, project, "VP-001") == []


def test_a_missing_citations_artifact_is_reported(tmp_path: Path, project: Path):
    issues = verify_evidence("code_review", tmp_path, project, "VP-001")

    assert [i.kind for i in issues] == ["missing_artifact"]
    assert CITATIONS_FILENAME in issues[0].detail


def test_a_missing_checkpoints_artifact_is_reported(tmp_path: Path, project: Path):
    issues = verify_evidence("ui_validation", tmp_path, project, "VP-001")

    assert [i.kind for i in issues] == ["missing_artifact"]
    assert CHECKPOINTS_FILENAME in issues[0].detail


def test_an_unparseable_artifact_is_reported(tmp_path: Path, project: Path):
    directory = artifact_dir_for(tmp_path, "VP-001")
    directory.mkdir(parents=True)
    (directory / CITATIONS_FILENAME).write_text("{not json")

    assert [i.kind for i in verify_evidence(
        "code_review", tmp_path, project, "VP-001"
    )] == ["missing_artifact"]


def test_a_supported_passed_verdict_is_left_alone(tmp_path: Path, project: Path):
    _write(tmp_path, "VP-001", CITATIONS_FILENAME, {"citations": [{
        "file": "src/engine.rs", "line": 2,
        "snippet": "let metric = find_metric(&bars);",
    }]})
    verdict = {"status": "PASSED", "reasons": ["ok"], "evidence": {"a": 1}}

    checked = apply_to_verdict(verdict, "code_review", tmp_path, project, "VP-001")

    assert checked["status"] == "PASSED"
    assert checked["reasons"] == ["ok"]


def test_an_unsupported_passed_verdict_is_downgraded(tmp_path: Path, project: Path):
    verdict = {"status": "PASSED", "reasons": ["looks good"], "evidence": {}}

    checked = apply_to_verdict(verdict, "code_review", tmp_path, project, "VP-001")

    assert checked["status"] == "FAILED"
    assert "无法核对" in checked["actual_result"]
    assert any("missing_artifact" in r for r in checked["reasons"])
    assert checked["evidence"]["evidence_check"]["issues"]


def test_a_failed_verdict_is_left_completely_alone(tmp_path: Path, project: Path):
    """Only PASSED is inspected. A FAILED stands on its own — and a
    reviewer reporting missing code often has no line to cite — so
    touching it would only add noise to every failure path, including
    the exception paths that never had a chance to write an artifact."""
    verdict = {"status": "FAILED", "reasons": ["not implemented"], "evidence": {}}

    checked = apply_to_verdict(verdict, "code_review", tmp_path, project, "VP-001")

    assert checked is verdict


def test_a_wrong_citation_downgrades_a_passed_verdict(tmp_path: Path, project: Path):
    _write(tmp_path, "VP-001", CITATIONS_FILENAME, {"citations": [{
        "file": "src/engine.rs", "line": 2,
        "snippet": "let manufactured_evidence = true;",
    }]})

    checked = apply_to_verdict(
        {"status": "PASSED", "reasons": [], "evidence": {}},
        "code_review", tmp_path, project, "VP-001",
    )

    assert checked["status"] == "FAILED"
    assert any(
        "snippet_mismatch" in r for r in checked["reasons"]
    )


def test_the_original_verdict_is_not_mutated(tmp_path: Path, project: Path):
    verdict = {"status": "PASSED", "reasons": ["ok"], "evidence": {}}

    apply_to_verdict(verdict, "code_review", tmp_path, project, "VP-001")

    assert verdict["status"] == "PASSED"
    assert verdict["reasons"] == ["ok"]


def test_apply_to_verdict_passes_non_dicts_through(tmp_path: Path, project: Path):
    assert apply_to_verdict(None, "code_review", tmp_path, project, "V") is None


def test_checkpoints_artifact_end_to_end(tmp_path: Path, project: Path):
    _write(tmp_path, "VP-002", CHECKPOINTS_FILENAME, {"checkpoints": [{
        "selector": "canvas", "expected": "rendered", "actual": "rendered",
        "passed": True,
    }]})

    checked = apply_to_verdict(
        {"status": "PASSED", "reasons": [], "evidence": {}},
        "ui_validation", tmp_path, project, "VP-002",
    )

    assert checked["status"] == "PASSED"


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------


def _make_agent(tmp_path: Path):
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )


def test_apply_evidence_check_downgrades_in_the_agent(tmp_path: Path):
    agent = _make_agent(tmp_path)
    vp = {"id": "VP-001", "verification_method": "code_review"}

    checked = agent._apply_evidence_check(
        vp, {"status": "PASSED", "reasons": ["looks good"], "evidence": {}},
    )

    assert checked["status"] == "FAILED"


def test_apply_evidence_check_leaves_other_methods_alone(tmp_path: Path):
    agent = _make_agent(tmp_path)
    verdict = {"status": "PASSED", "reasons": [], "evidence": {}}
    vp = {"id": "VP-001", "verification_method": "api_test"}

    assert agent._apply_evidence_check(vp, verdict) is verdict


def test_apply_evidence_check_accepts_resolvable_evidence(tmp_path: Path):
    agent = _make_agent(tmp_path)
    (agent.project_dir / "src").mkdir(parents=True, exist_ok=True)
    (agent.project_dir / "src" / "a.py").write_text("value = 1\n")
    _write(agent.plan_dir, "VP-001", CITATIONS_FILENAME, {"citations": [{
        "file": "src/a.py", "line": 1, "snippet": "value = 1",
    }]})
    verdict = {"status": "PASSED", "reasons": ["ok"], "evidence": {}}

    checked = agent._apply_evidence_check(
        {"id": "VP-001", "verification_method": "code_review"}, verdict,
    )

    assert checked["status"] == "PASSED"


def test_apply_evidence_check_never_raises(tmp_path: Path, monkeypatch):
    import verification_evidence

    agent = _make_agent(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("evidence check exploded")

    monkeypatch.setattr(verification_evidence, "apply_to_verdict", _boom)

    verdict = {"status": "PASSED", "reasons": [], "evidence": {}}
    checked = agent._apply_evidence_check(
        {"id": "VP-001", "verification_method": "code_review"}, verdict,
    )

    assert checked is verdict


def test_the_runner_injects_the_artifact_dir(tmp_path: Path):
    """The sub-agent can only write the artifact if it is told where."""
    agent = _make_agent(tmp_path)
    seen: list[dict] = []

    async def _capture(vp):
        seen.append(dict(vp))
        return {"status": "PASSED", "reasons": [], "evidence": {}}

    agent._run_single_vp_async = _capture  # type: ignore[assignment]
    plan = {"verification_points": [{
        "id": "VP-009", "title": "t", "verification_method": "code_review",
    }]}
    executor = agent._build_verification_executor(plan)

    import asyncio

    asyncio.run(executor.sub_agent_runner(executor.verification_plan["vps"][0]))

    assert seen and seen[0]["evidence_artifact_dir"] == str(
        agent.plan_dir / "vp_artifacts" / "VP-009"
    )


# ---------------------------------------------------------------------------
# The prompt contract
# ---------------------------------------------------------------------------


def test_code_review_prompt_requires_citations():
    from verification_subagent import VerificationSubAgent

    agent = VerificationSubAgent(method="code_review")
    parts = agent._evidence_contract({
        "verification_method": "code_review",
        "evidence_artifact_dir": "/tmp/artifacts/VP-001",
    })

    text = "\n".join(parts)
    assert "citations.json" in text
    assert "/tmp/artifacts/VP-001" in text
    assert "downgraded to FAILED" in text


def test_ui_validation_prompt_requires_checkpoints():
    from verification_subagent import VerificationSubAgent

    agent = VerificationSubAgent(method="ui_validation")
    text = "\n".join(agent._evidence_contract({
        "verification_method": "ui_validation",
        "evidence_artifact_dir": "/tmp/artifacts/VP-002",
    }))

    assert "checkpoints.json" in text
    assert "/tmp/artifacts/VP-002" in text


def test_no_contract_block_without_an_artifact_dir():
    from verification_subagent import VerificationSubAgent

    agent = VerificationSubAgent(method="code_review")

    assert agent._evidence_contract({"verification_method": "code_review"}) == []


def test_api_test_gets_no_contract_block():
    """api_test's basis is produced by the framework, not by a sub-agent."""
    from verification_subagent import VerificationSubAgent

    agent = VerificationSubAgent(method="api_test")

    assert agent._evidence_contract({
        "verification_method": "api_test",
        "evidence_artifact_dir": "/tmp/artifacts/VP-003",
    }) == []


# ---------------------------------------------------------------------------
# evidence_command — supporting evidence, never the verdict
# ---------------------------------------------------------------------------


def test_evidence_command_result_is_attached_without_changing_status(
    tmp_path: Path, project: Path
):
    """The whole point of retiring the VP ``test_command``: a command's
    exit code must not be able to grade the VP."""
    verdict = {"status": "PASSED", "reasons": ["reviewer says ok"], "evidence": {}}
    vp = {"id": "VP-001", "evidence_command": "exit 1"}

    checked = attach_evidence_command(verdict, vp, project)

    assert checked["status"] == "PASSED", "a failing evidence command must not grade"
    assert any("[evidence]" in r for r in checked["reasons"])
    assert checked["evidence"]["evidence_command"]["exit_code"] == 1


def test_a_passing_evidence_command_does_not_upgrade_a_failure(
    tmp_path: Path, project: Path
):
    verdict = {"status": "FAILED", "reasons": ["reviewer says broken"], "evidence": {}}
    vp = {"id": "VP-001", "evidence_command": "exit 0"}

    checked = attach_evidence_command(verdict, vp, project)

    assert checked["status"] == "FAILED"


def test_no_evidence_command_leaves_the_verdict_untouched(project: Path):
    verdict = {"status": "PASSED", "reasons": [], "evidence": {}}

    assert attach_evidence_command(verdict, {"id": "VP-001"}, project) is verdict
    assert attach_evidence_command(
        verdict, {"id": "VP-001", "evidence_command": "   "}, project,
    ) is verdict


def test_the_command_runs_in_the_project_dir(project: Path):
    vp = {"id": "VP-001", "evidence_command": "pwd"}

    checked = attach_evidence_command(
        {"status": "PASSED", "reasons": [], "evidence": {}}, vp, project,
    )

    assert str(project) in checked["evidence"]["evidence_command"]["stdout"]


def test_a_failing_evidence_command_is_reported_not_raised(project: Path):
    vp = {"id": "VP-001", "evidence_command": "definitely-not-a-command-xyz"}

    checked = attach_evidence_command(
        {"status": "PASSED", "reasons": [], "evidence": {}}, vp, project,
    )

    assert checked["status"] == "PASSED"
    assert checked["evidence"]["evidence_command"]["exit_code"] not in (0, None)


def test_evidence_command_output_is_bounded(project: Path):
    vp = {"id": "VP-001", "evidence_command": "yes x | head -c 200000"}

    checked = attach_evidence_command(
        {"status": "PASSED", "reasons": [], "evidence": {}}, vp, project,
    )

    payload = checked["evidence"]["evidence_command"]
    assert len(payload["stdout"]) <= MAX_EVIDENCE_OUTPUT_CHARS


def test_evidence_command_timeout_is_reported(project: Path):
    vp = {"id": "VP-001", "evidence_command": "sleep 5"}

    outcome = run_evidence_command(vp["evidence_command"], project, timeout_seconds=1)

    assert outcome.timed_out is True
    assert "timed out" in outcome.error
    assert outcome.exit_code is None


def test_the_agent_attaches_a_declared_evidence_command(tmp_path: Path):
    agent = _make_agent(tmp_path)
    (agent.project_dir / "marker.txt").write_text("hello\n")
    vp = {
        "id": "VP-001",
        "verification_method": "api_test",
        "evidence_command": "cat marker.txt",
    }

    checked = agent._attach_evidence_command(vp, {"status": "PASSED"})

    assert "hello" in checked["evidence"]["evidence_command"]["stdout"]


def test_the_agent_never_raises_on_a_broken_evidence_command(tmp_path: Path, monkeypatch):
    import verification_evidence

    agent = _make_agent(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("evidence exploded")

    monkeypatch.setattr(verification_evidence, "attach_evidence_command", _boom)

    verdict = {"status": "PASSED"}
    checked = agent._attach_evidence_command(
        {"id": "VP-001", "evidence_command": "true"}, verdict,
    )

    assert checked is verdict
