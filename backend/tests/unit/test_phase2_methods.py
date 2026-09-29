"""The two Phase-2 methods — ``e2e`` and ``full_ci`` (2026-09-18, D2).

Why these two exist
-------------------
The 2026-09-16 two-phase decision says the plan ends with two 全量关卡:
full Nightly CI first, then full E2E. When the VP judgment rework deleted
``test_command`` the E2E gate survived (``ui_validation`` can drive a
browser) but the CI gate lost every field that could carry it — which is
why a plan can regenerate without its Phase-2 Nightly CI gate — an
acceptance criterion the PRD names explicitly.

These tests pin the seams:
  * the vocabulary (both are supported, both are Phase-2-only, both have
    templates, ``full_ci`` is framework-executed like ``api_test``);
  * ``e2e`` shares the puppeteer execution path, so its verdict comes
    from checkpoints rather than from the LLM's prose;
  * ``e2e`` carries the same evidence contract as ``ui_validation`` —
    a PASSED with no ``checkpoints.json`` is downgraded;
  * the executor-schema converter carries ``ci_entry`` through, or
    ``_run_full_ci_vp`` sees a VP with no judgment input.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_subagent import (  # noqa: E402
    FRAMEWORK_METHODS,
    PHASE_2_ONLY_METHODS,
    SUPPORTED_METHODS,
    MethodTemplateRegistry,
    VerificationSubAgent,
)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class TestVocabulary:
    def test_both_phase2_methods_are_supported(self):
        assert "e2e" in SUPPORTED_METHODS
        assert "full_ci" in SUPPORTED_METHODS

    def test_registry_is_total_over_supported_methods(self):
        assert set(MethodTemplateRegistry.supported_methods()) == set(SUPPORTED_METHODS)

    def test_every_supported_method_has_a_template(self):
        for method in SUPPORTED_METHODS:
            assert MethodTemplateRegistry.get_template(method).strip()

    def test_full_ci_is_framework_executed(self):
        assert "full_ci" in FRAMEWORK_METHODS

    def test_e2e_is_not_framework_executed(self):
        # E2E needs a real browser driven by an agent — it is the whole
        # reason `command_run` was rejected as a name.
        assert "e2e" not in FRAMEWORK_METHODS

    def test_phase2_only_methods_are_exactly_e2e_and_full_ci(self):
        assert set(PHASE_2_ONLY_METHODS) == {"e2e", "full_ci"}

    def test_retired_methods_stay_retired(self):
        for retired in ("automated_test", "manual_check"):
            assert retired not in SUPPORTED_METHODS
            with pytest.raises(ValueError):
                MethodTemplateRegistry.get_template(retired)


# ---------------------------------------------------------------------------
# e2e shares the puppeteer execution path
# ---------------------------------------------------------------------------


def _subagent(method: str) -> VerificationSubAgent:
    return VerificationSubAgent(method=method)


class TestE2eVerdict:
    def test_all_checkpoints_passed_is_passed(self):
        status, reasons, evidence = _subagent("e2e")._compute_verdict(
            "e2e",
            {"checkpoints": [{"selector": "#a", "passed": True}]},
        )
        assert status == "PASSED"
        assert evidence["passed_count"] == 1

    def test_one_failed_checkpoint_is_failed(self):
        status, _, evidence = _subagent("e2e")._compute_verdict(
            "e2e",
            {"checkpoints": [
                {"selector": "#a", "passed": True},
                {"selector": "#b", "passed": False},
            ]},
        )
        assert status == "FAILED"
        assert evidence["failed_count"] == 1

    def test_no_checkpoints_is_failed(self):
        status, reasons, _ = _subagent("e2e")._compute_verdict("e2e", {})
        assert status == "FAILED"
        assert any("no checkpoints" in r for r in reasons)

    def test_e2e_and_ui_validation_agree(self):
        payload = {"checkpoints": [{"selector": "#a", "passed": True}]}
        assert (
            _subagent("e2e")._compute_verdict("e2e", payload)[0]
            == _subagent("ui_validation")._compute_verdict("ui_validation", payload)[0]
        )


# ---------------------------------------------------------------------------
# e2e evidence contract
# ---------------------------------------------------------------------------


class TestE2eEvidenceContract:
    def test_contract_demands_checkpoints_json(self, tmp_path):
        block = "\n".join(_subagent("e2e")._evidence_contract({
            "verification_method": "e2e",
            "evidence_artifact_dir": str(tmp_path / "vp_artifacts" / "VP-017"),
        }))
        assert "checkpoints.json" in block
        assert str(tmp_path / "vp_artifacts" / "VP-017") in block

    def test_missing_checkpoints_artifact_is_an_issue(self, tmp_path):
        from verification_evidence import verify_evidence

        issues = verify_evidence(
            "e2e", tmp_path / "plan", tmp_path / "project", "VP-017",
        )
        assert issues and issues[0].kind == "missing_artifact"

    def test_unsupported_passed_is_downgraded(self, tmp_path):
        from verification_evidence import apply_to_verdict

        verdict = apply_to_verdict(
            {"status": "PASSED", "reasons": []},
            "e2e", tmp_path / "plan", tmp_path / "project", "VP-017",
        )
        assert verdict["status"] == "FAILED"
        assert any("checkpoints.json" in r for r in verdict["reasons"])

    def test_a_real_checkpoints_file_holds_the_pass(self, tmp_path):
        from verification_evidence import apply_to_verdict

        plan_dir = tmp_path / "plan"
        artifact = plan_dir / "vp_artifacts" / "VP-017"
        artifact.mkdir(parents=True)
        (artifact / "checkpoints.json").write_text(
            json.dumps({"checkpoints": [
                {"selector": "#chart", "expected": "canvas", "actual": "canvas",
                 "passed": True},
            ]}),
            encoding="utf-8",
        )
        verdict = apply_to_verdict(
            {"status": "PASSED", "reasons": []},
            "e2e", plan_dir, tmp_path / "project", "VP-017",
        )
        assert verdict["status"] == "PASSED"


# ---------------------------------------------------------------------------
# full_ci wiring into the agent
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


def _ci_vp(**over) -> dict:
    vp = {
        "id": "VP-017",
        "title": "【全量】Nightly CI",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "phase_order": 1,
        "ci_entry": "exit 0",
    }
    vp.update(over)
    return vp


class TestFullCiWiring:
    def test_conversion_carries_ci_entry(self):
        from verification_agent import VerificationAgent

        converted = VerificationAgent._convert_plan_to_executor_schema(
            {"verification_points": [_ci_vp()]}
        )
        vp = converted["vps"][0]
        assert vp["ci_entry"] == "exit 0"
        assert "ci_timeout_seconds" in vp

    def test_passing_entry_is_passed(self, tmp_path):
        verdict = _make_agent(tmp_path)._run_full_ci_vp(_ci_vp())
        assert verdict["status"] == "PASSED"
        assert verdict["evidence"]["exit_code"] == 0

    def test_failing_entry_is_failed(self, tmp_path):
        verdict = _make_agent(tmp_path)._run_full_ci_vp(_ci_vp(ci_entry="exit 7"))
        assert verdict["status"] == "FAILED"
        assert verdict["evidence"]["exit_code"] == 7

    def test_verdict_has_the_executor_shape(self, tmp_path):
        verdict = _make_agent(tmp_path)._run_full_ci_vp(_ci_vp())
        assert set(verdict) == {"status", "reasons", "evidence"}

    def test_audit_artifact_is_written(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent._run_full_ci_vp(_ci_vp())
        artifact = agent.plan_dir / "vp_artifacts" / "VP-017" / "ci_output.json"
        assert artifact.exists()
        assert json.loads(artifact.read_text(encoding="utf-8"))["exit_code"] == 0

    def test_narrowed_entry_fails_without_running(self, tmp_path):
        marker = tmp_path / "project" / "ran.txt"
        verdict = _make_agent(tmp_path)._run_full_ci_vp(
            _ci_vp(ci_entry="touch ran.txt; pytest tests/test_x.py")
        )
        assert verdict["status"] == "FAILED"
        assert "收窄" in " ".join(verdict["reasons"])
        assert not marker.exists()
