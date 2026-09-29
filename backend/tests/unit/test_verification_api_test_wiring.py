"""Wiring tests for the framework-executed ``api_test`` path (2026-09-18).

The deterministic runner itself is covered in
``test_verification_api_runner.py``. This file covers the seams that
were easy to get wrong:

  * ``_convert_plan_to_executor_schema`` must carry ``request`` /
    ``assertions`` through — otherwise the runner sees a VP with no
    judgment inputs and grades it on a schema error.
  * ``_api_schema_report`` must surface malformed ``api_test`` VPs so the
    plan-regeneration loop can fix them before a round is burned.
  * ``_run_api_test_vp`` must produce the executor's verdict shape and
    write the audit artifact.
  * the ``echo`` filler must no longer be applied to ``api_test``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _make_agent(tmp_path: Path):
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )


def _api_vp(vp_id: str = "VP-001", **over) -> dict:
    vp = {
        "id": vp_id,
        "title": "接口契约",
        "verification_method": "api_test",
        "request": {"method": "GET", "url": "http://127.0.0.1:1/x"},
        "assertions": [{"name": "ok", "status": 200}],
    }
    vp.update(over)
    return vp


# ---------------------------------------------------------------------------
# Executor-schema conversion
# ---------------------------------------------------------------------------


def test_conversion_carries_request_and_assertions():
    from verification_agent import VerificationAgent

    plan = {"verification_points": [_api_vp()]}

    converted = VerificationAgent._convert_plan_to_executor_schema(plan)

    [vp] = converted["vps"]
    assert vp["method"] == "api_test"
    assert vp["request"] == {"method": "GET", "url": "http://127.0.0.1:1/x"}
    assert vp["assertions"] == [{"name": "ok", "status": 200}]


def test_conversion_leaves_non_api_vps_without_the_fields():
    from verification_agent import VerificationAgent

    plan = {"verification_points": [
        {"id": "VP-002", "title": "审查", "verification_method": "code_review"},
    ]}

    converted = VerificationAgent._convert_plan_to_executor_schema(plan)

    [vp] = converted["vps"]
    assert vp["request"] is None
    assert vp["assertions"] is None


# ---------------------------------------------------------------------------
# _api_schema_report
# ---------------------------------------------------------------------------


def test_api_schema_report_flags_a_vp_with_no_assertions(tmp_path: Path):
    agent = _make_agent(tmp_path)
    plan = {"verification_points": [_api_vp(assertions=[])]}

    report = agent._api_schema_report(plan)

    assert [r["id"] for r in report] == ["VP-001"]
    assert any("assertions" in d for d in report[0]["issues"])


def test_api_schema_report_ignores_a_valid_vp(tmp_path: Path):
    agent = _make_agent(tmp_path)

    assert agent._api_schema_report({"verification_points": [_api_vp()]}) == []


def test_api_schema_report_ignores_other_methods(tmp_path: Path):
    agent = _make_agent(tmp_path)
    plan = {"verification_points": [
        {"id": "VP-009", "verification_method": "code_review"},
        {"id": "VP-010", "verification_method": "ui_validation"},
    ]}

    assert agent._api_schema_report(plan) == []


def test_api_schema_report_survives_malformed_plans(tmp_path: Path):
    agent = _make_agent(tmp_path)

    assert agent._api_schema_report(None) == []
    assert agent._api_schema_report({"verification_points": "nope"}) == []
    assert agent._api_schema_report({"verification_points": [None, 3]}) == []


# ---------------------------------------------------------------------------
# _run_api_test_vp
# ---------------------------------------------------------------------------


def test_run_api_test_vp_returns_a_failed_verdict_for_an_unreachable_url(
    tmp_path: Path,
):
    """Port 1 on loopback refuses immediately — no network, deterministic."""
    agent = _make_agent(tmp_path)

    verdict = agent._run_api_test_vp(_api_vp())

    assert verdict["status"] == "FAILED"
    assert verdict["reasons"]
    assert "请求未能完成" in verdict["reasons"][0]


def test_run_api_test_vp_writes_the_audit_artifact(tmp_path: Path):
    agent = _make_agent(tmp_path)

    agent._run_api_test_vp(_api_vp())

    artifact = agent.plan_dir / "vp_artifacts" / "VP-001" / "api_response.json"
    assert artifact.exists()
    payload = json.loads(artifact.read_text())
    assert payload["vp_id"] == "VP-001"
    assert payload["request"]["method"] == "GET"


def test_run_api_test_vp_grades_a_malformed_vp_as_schema_failure(tmp_path: Path):
    agent = _make_agent(tmp_path)
    vp = {"id": "VP-007", "verification_method": "api_test"}

    verdict = agent._run_api_test_vp(vp)

    assert verdict["status"] == "FAILED"
    assert verdict["evidence"]["schema_issues"]


def test_run_api_test_vp_never_raises(tmp_path: Path, monkeypatch):
    agent = _make_agent(tmp_path)

    import verification_api_runner as runner

    def _boom(*args, **kwargs):
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(runner, "run_api_verification", _boom)

    verdict = agent._run_api_test_vp(_api_vp())

    assert verdict["status"] == "FAILED"
    assert "crashed" in verdict["reasons"][0]


# ---------------------------------------------------------------------------
# The filler must not come back for api_test
# ---------------------------------------------------------------------------


def test_echo_filler_is_not_applied_to_api_test(tmp_path: Path):
    agent = _make_agent(tmp_path)
    plan = {"verification_points": [
        {"id": "VP-001", "title": "t", "verification_method": "api_test"},
    ]}

    agent._enrich_vps_with_execution_metadata(plan)

    assert not plan["verification_points"][0].get("test_command")


def test_no_echo_filler_for_any_method(tmp_path: Path):
    """2026-09-18: the filler is gone for *every* method, not just
    api_test. It exited 0 while proving nothing, and the zero-tests gate
    then failed the VP — every VP generated that way was born dead."""
    agent = _make_agent(tmp_path)
    plan = {"verification_points": [
        {"id": f"VP-00{i}", "title": "t", "verification_method": method}
        for i, method in enumerate(
            ("api_test", "code_review", "ui_validation"), start=1
        )
    ]}

    agent._enrich_vps_with_execution_metadata(plan)

    for vp in plan["verification_points"]:
        assert not vp.get("test_command"), vp


# ---------------------------------------------------------------------------
# Regeneration guidance
# ---------------------------------------------------------------------------


def test_violation_guidance_covers_the_api_family(tmp_path: Path):
    agent = _make_agent(tmp_path)

    text = agent._violation_fix_guidance([], [{"id": "VP-001"}])

    assert "api_test" in text
    assert "json_path" in text
    assert "收窄" not in text  # the unbounded family's wording must not leak


def test_violation_guidance_stays_quiet_when_no_api_violations(tmp_path: Path):
    agent = _make_agent(tmp_path)

    text = agent._violation_fix_guidance([], [])

    assert "json_path" not in text


# ---------------------------------------------------------------------------
# target_url plumbing (2026-09-18)
#
# A ui_validation VP is *required* by the planning prompt to declare a
# target_url, but the field never reached either the executor schema or
# the sub-agent prompt — the reviewer had to infer the page from prose.
# ---------------------------------------------------------------------------


def test_conversion_carries_target_url():
    from verification_agent import VerificationAgent

    plan = {"verification_points": [{
        "id": "VP-001", "title": "页面", "verification_method": "ui_validation",
        "target_url": "http://127.0.0.1:3000/data-viewer",
    }]}

    converted = VerificationAgent._convert_plan_to_executor_schema(plan)

    assert converted["vps"][0]["target_url"] == (
        "http://127.0.0.1:3000/data-viewer"
    )


def test_the_vp_prompt_tells_the_reviewer_which_url_to_open():
    from verification_subagent import VerificationSubAgent

    agent = VerificationSubAgent(method="ui_validation")
    prompt = agent._build_prompt({
        "id": "VP-001", "title": "页面", "expected_result": "图表可见",
        "verification_method": "ui_validation",
        "target_url": "http://127.0.0.1:3000/data-viewer",
    }, attempt=0)

    assert "Target URL: http://127.0.0.1:3000/data-viewer" in prompt


def test_the_vp_prompt_omits_a_missing_target_url():
    from verification_subagent import VerificationSubAgent

    agent = VerificationSubAgent(method="code_review")
    prompt = agent._build_prompt({
        "id": "VP-001", "verification_method": "code_review",
    }, attempt=0)

    assert "Target URL:" not in prompt


def test_the_vp_prompt_names_an_evidence_command_but_says_it_does_not_grade():
    from verification_subagent import VerificationSubAgent

    agent = VerificationSubAgent(method="code_review")
    prompt = agent._build_prompt({
        "id": "VP-001", "verification_method": "code_review",
        "evidence_command": "cargo test --lib anchor",
    }, attempt=0)

    assert "cargo test --lib anchor" in prompt
    assert "does NOT decide the verdict" in prompt


# ---------------------------------------------------------------------------
# api_test schema gate — hard reject when retry budget cannot produce a
# runnable plan. Earlier rounds kept the "best of bad" plan and let the
# runner mark the VP FAILED at execution time; an api_test with malformed
# assertions literally cannot be graded, so proceeding silently is worse
# than refusing. The gate must raise rather than save.
# ---------------------------------------------------------------------------


def _verification_agent_with_mock_coding_tool(tmp_path: Path, mock):
    """Build a VerificationAgent wired to the supplied mock coding tool.

    The standard ``verification_agent`` fixture in test_verification_agent
    builds one with a single happy-path return; here we need to drive
    every ``query_json`` call (multiple retries, possibly all returning
    the same malformed plan) so the test asserts the gate fires on
    exactly the retry-exhausted boundary.
    """
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)

    class _StubCodingTool:
        query_json = mock

    return VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir,
        coding_tool=_StubCodingTool(),
    )


def test_api_schema_violations_fail_loudly_after_retries_exhausted(tmp_path):
    """A plan whose api_test VPs fail schema validation must NOT be
    saved as a 'best of bad' fallback when the LLM keeps reproducing
    the same defect. The runner literally cannot grade a VP with an
    assertion key outside SUBJECT_KEYS, so persisting the plan and
    letting execution proceed is worse than refusing outright.

    The exception this raises is documented as the signal: the
    orchestrator's only correct action is to surface the schema issues
    to the operator, NOT to mark the VP FAILED at run time.
    """
    from unittest.mock import MagicMock

    mock = MagicMock()
    # Every retry returns the same malformed plan — the LLM cannot
    # escape the trap on its own, which is exactly the case the brief
    # calls out (VP-007 walked all the way to execution before being
    # marked FAILED).
    malformed_plan = {
        "verification_points": [
            {
                "id": "VP-007",
                "title": "traversal kill",
                "verification_method": "api_test",
                "request": {
                    "method": "GET",
                    "url": "{{svc.api.url}}/api/plan/..%2F..%2Fetc%2Fpasswd/status",
                },
                # ``body_doesnt_contain`` is NOT in SUBJECT_KEYS — the
                # runner cannot judge this assertion under any name.
                "assertions": [
                    {"name": "status", "status": 404},
                    {"name": "no leak",
                     "body_doesnt_contain": "root:x:0:0"},
                ],
            },
        ],
    }
    mock.return_value = malformed_plan

    agent = _verification_agent_with_mock_coding_tool(tmp_path, mock)

    with pytest.raises(Exception) as excinfo:
        agent.generate_verification_plan(retry_llm=2)

    # Every retry was tried — and the gate then refused, which is the
    # whole point. Saving the plan and letting execution mark the VP
    # FAILED is the regression this test pins.
    assert mock.call_count == 2, (
        f"expected the LLM to be called retry_llm=2 times before the "
        f"gate fired; got {mock.call_count}"
    )
    message = str(excinfo.value).lower()
    assert (
        "schema" in message
        or "api_test" in message
        or "assertion" in message
        or "body_doesnt_contain" in message
    ), (
        f"hard-rejection message should name the failing schema or the "
        f"unknown assertion key, got: {excinfo.value!r}"
    )

    # And nothing was persisted to disk — the next round must start
    # from a clean slate, not from the rejected-by-gate plan. The plan
    # directory may contain sibling scaffolding (screenshots, logs)
    # that the agent creates independently, so check for the actual
    # plan artifact, not the whole directory.
    plan_file = tmp_path / "plan" / "verification_plan.json"
    assert not plan_file.exists(), (
        f"verification_plan.json must not exist after a hard rejection; "
        f"saving it would let the next round consume an unrunnable plan"
    )


def test_api_schema_violations_free_other_methods(tmp_path):
    """Service-reference and gate-gap violations can still fall back
    to the 'best of bad' plan — those don't keep the runner from
    grading VPs the way they are written. Only api_test schema
    violations trigger the hard reject, because ONLY they make a VP
    literally un-runnable.
    """
    from unittest.mock import MagicMock

    mock = MagicMock()
    plan_with_service_violation_only = {
        "verification_points": [
            {
                "id": "VP-001",
                "title": "valid api_test",
                "verification_method": "api_test",
                "request": {"method": "GET",
                            "url": "{{svc.api.url}}/api/items?limit=10"},
                "assertions": [
                    {"name": "status", "status": 200},
                ],
            },
        ],
        # Service violations are taken from :meth:`_service_reference_report`,
        # not from the LLM output, so they live on a different code path.
    }
    mock.return_value = plan_with_service_violation_only

    agent = _verification_agent_with_mock_coding_tool(tmp_path, mock)

    # Service violations alone do NOT raise — only api_test schema
    # violations do. The plan is saved (with annotations) so the
    # rest of the contract can keep working.
    plan = agent.generate_verification_plan(retry_llm=1)

    assert plan is not None
    assert "verification_points" in plan


def test_disk_plan_with_api_schema_violations_is_rejected_on_load(tmp_path):
    """Loading a hand-edited (or stale) plan with api_test schema
    violations from disk must NOT silently re-use it — the same
    hard-reject contract applies. The brief calls out "generation /
    loading" both, so the cache path is also a gate.
    """
    from unittest.mock import MagicMock

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)

    bad_disk_plan = {
        "verification_points": [
            {
                "id": "VP-100",
                "title": "stale",
                "verification_method": "api_test",
                "request": {"method": "GET", "url": "http://x/y"},
                "assertions": [{"name": "bad",
                                 "totally_made_up_subject": "hi"}],
            },
        ],
    }
    on_disk = plan_dir / "verification_plan.json"
    on_disk.write_text(json.dumps(bad_disk_plan), encoding="utf-8")

    mock = MagicMock()  # LLM should never be called on the load path.

    agent = _verification_agent_with_mock_coding_tool(tmp_path, mock)

    with pytest.raises(Exception):
        agent.generate_verification_plan(retry_llm=1)

    # The load path short-circuits before Phase-1; the LLM is not
    # called even once.
    assert mock.call_count == 0


# ---------------------------------------------------------------------------
# The regeneration guidance is the third surface that must know the whole
# vocabulary (2026-09-27).
#
# The plan-generation prompt, ``verification_api_runner.SUBJECT_KEYS`` and
# this guidance all have to agree. The guidance is the one a plan is
# re-written from *after* it was rejected, so a subject missing here is
# worse than missing from the prompt: the regenerated plan is asked to fix
# an assertion it is not told how to write, and it re-emits the same
# rejected spelling — which is exactly how the VP-007 loop failed to
# converge across four rounds.
# ---------------------------------------------------------------------------


def test_violation_guidance_advertises_every_subject_the_runner_accepts(tmp_path: Path):
    from verification_api_runner import SUBJECT_KEYS

    text = _make_agent(tmp_path)._violation_fix_guidance([], [{"id": "VP-001"}])

    for subject in SUBJECT_KEYS:
        assert f'"{subject}"' in text, (
            f"the api_test regeneration guidance does not name the subject "
            f"{subject!r}; the runner accepts it, so a plan rejected for "
            f"using it cannot be regenerated into a valid one."
        )
