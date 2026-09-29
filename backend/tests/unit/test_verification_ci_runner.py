"""Unit tests for the full CI gate runner (2026-09-18 Phase 2 gate).

Covers:
  * ``looks_narrowed`` — the anti-degeneration check that keeps
    ``ci_entry`` from decaying back into a general-purpose
    ``test_command`` (single test file / ``-k`` / ``::`` / cargo
    selectors).
  * ``validate_vp`` — required field, narrowing rejection, and the
    repo-path existence check.
  * ``run_ci_verification`` — exit 0 → PASSED; non-zero / timeout /
    missing entry → FAILED with the reason; the audit artifact.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_ci_runner import (  # noqa: E402
    DEFAULT_CI_TIMEOUT_SECONDS,
    STATUS_FAILED,
    STATUS_PASSED,
    ci_timeout,
    looks_narrowed,
    run_ci_verification,
    validate_vp,
)


def _vp(entry, **extra):
    vp = {
        "id": "VP-901",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "phase_order": 1,
        "ci_entry": entry,
    }
    vp.update(extra)
    return vp


# ---------------------------------------------------------------------------
# looks_narrowed — the anti-degeneration check
# ---------------------------------------------------------------------------


class TestLooksNarrowed:
    def test_whole_repo_entry_is_not_narrowed(self):
        assert looks_narrowed("python scripts/ci_local.py") is None

    def test_cargo_workspace_is_not_narrowed(self):
        assert looks_narrowed("cargo test --workspace --all-features") is None

    def test_bare_pytest_is_not_narrowed(self):
        assert looks_narrowed("pytest tests/") is None

    def test_pytest_keyword_selector_is_narrowed(self):
        reason = looks_narrowed("pytest tests/ -k divergence")
        assert reason and "-k" in reason

    def test_pytest_markexpr_is_narrowed(self):
        assert looks_narrowed("pytest -m slow tests/") is not None

    def test_pytest_single_file_is_narrowed(self):
        reason = looks_narrowed("pytest tests/test_signal.py")
        assert reason and "单个测试文件" in reason

    def test_pytest_node_id_is_narrowed(self):
        assert looks_narrowed("pytest tests/test_x.py::TestY::test_z") is not None

    def test_cargo_named_test_target_is_narrowed(self):
        # `cargo test --test divergence` runs ONE integration target.
        reason = looks_narrowed("cargo test --test divergence")
        assert reason and "--test" in reason

    def test_cargo_lib_only_is_narrowed(self):
        assert looks_narrowed("cargo test --lib") is not None

    def test_rust_single_test_file_is_narrowed(self):
        assert looks_narrowed("cargo test native_ext/tests/foo_test.rs") is not None

    def test_playwright_spec_file_is_narrowed(self):
        reason = looks_narrowed("npx playwright test tests/e2e/tooltip.spec.ts")
        assert reason and "单个测试文件" in reason

    def test_whole_playwright_suite_is_not_narrowed(self):
        assert looks_narrowed("npx playwright test") is None

    def test_empty_and_non_string_are_not_narrowed(self):
        assert looks_narrowed("") is None
        assert looks_narrowed("   ") is None

    def test_flag_with_equals_is_recognised(self):
        assert looks_narrowed("pytest tests/ --keyword=divergence") is not None


# ---------------------------------------------------------------------------
# validate_vp
# ---------------------------------------------------------------------------


class TestValidateVp:
    def test_valid_whole_repo_entry_passes(self):
        assert validate_vp(_vp("pytest tests/")) == []

    def test_missing_entry_is_an_issue(self):
        issues = validate_vp({"id": "VP-1", "verification_method": "full_ci"})
        assert len(issues) == 1
        assert "ci_entry" in issues[0].detail

    def test_blank_entry_is_an_issue(self):
        assert validate_vp(_vp("   ")) != []

    def test_narrowed_entry_is_an_issue(self):
        issues = validate_vp(_vp("pytest tests/ -k divergence"))
        assert any("收窄" in i.detail for i in issues)

    def test_narrowed_entry_keeps_the_schema_issue_even_without_project(self):
        # The narrowing rule must fire with or without a project dir —
        # it is a property of the entry, not of the tree.
        assert validate_vp(_vp("pytest tests/test_x.py"), project_dir=None) != []

    def test_missing_referenced_script_is_an_issue(self, tmp_path):
        issues = validate_vp(
            _vp("python scripts/ci_local.py"), project_dir=tmp_path,
        )
        assert any("不存在" in i.detail for i in issues)

    def test_existing_referenced_script_is_fine(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "ci_local.py").write_text("print('ok')\n")
        assert validate_vp(
            _vp("python scripts/ci_local.py"), project_dir=tmp_path,
        ) == []

    def test_absolute_paths_are_not_existence_checked(self, tmp_path):
        # An absolute path is outside the repo contract; we do not try
        # to resolve it against project_dir (which would always fail).
        assert validate_vp(
            _vp("/opt/bin/ci-gate"), project_dir=tmp_path,
        ) == []

    def test_non_dict_vp_is_reported(self):
        issues = validate_vp("not-a-vp")
        assert len(issues) == 1


# ---------------------------------------------------------------------------
# ci_timeout
# ---------------------------------------------------------------------------


class TestCiTimeout:
    def test_default_when_absent(self):
        assert ci_timeout({}) == DEFAULT_CI_TIMEOUT_SECONDS

    def test_explicit_value_wins(self):
        assert ci_timeout({"ci_timeout_seconds": 120}) == 120

    def test_garbage_falls_back_to_default(self):
        assert ci_timeout({"ci_timeout_seconds": "soon"}) == DEFAULT_CI_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# run_ci_verification — end to end against real commands
# ---------------------------------------------------------------------------


class TestRunCiVerification:
    def test_exit_zero_is_passed(self, tmp_path):
        result = run_ci_verification(
            _vp("exit 0"), project_dir=tmp_path,
        )
        assert result.status == STATUS_PASSED
        assert result.evidence["exit_code"] == 0

    def test_non_zero_exit_is_failed(self, tmp_path):
        result = run_ci_verification(
            _vp("exit 3"), project_dir=tmp_path,
        )
        assert result.status == STATUS_FAILED
        assert result.evidence["exit_code"] == 3
        assert any("退出码 3" in r for r in result.reasons)

    def test_failure_carries_the_output_tail(self, tmp_path):
        result = run_ci_verification(
            _vp("echo 'BOOM: 2 failed'; exit 1"), project_dir=tmp_path,
        )
        assert result.status == STATUS_FAILED
        joined = " ".join(result.reasons)
        assert "BOOM: 2 failed" in joined

    def test_timeout_is_failed_and_marked(self, tmp_path):
        result = run_ci_verification(
            _vp("sleep 5", ci_timeout_seconds=1), project_dir=tmp_path,
        )
        assert result.status == STATUS_FAILED
        assert result.evidence["timed_out"] is True
        assert any("超时" in r for r in result.reasons)

    def test_schema_failure_never_runs_the_command(self, tmp_path):
        marker = tmp_path / "ran.txt"
        # No `ci_entry` at all — the gate must fail on the schema and
        # must not execute anything.
        vp = {"id": "VP-901", "verification_method": "full_ci",
              "verification_phase": 2}
        result = run_ci_verification(vp, project_dir=tmp_path)
        assert result.status == STATUS_FAILED
        assert result.evidence["schema_issues"]
        assert not marker.exists()

    def test_narrowed_entry_is_rejected_before_running(self, tmp_path):
        marker = tmp_path / "ran.txt"
        result = run_ci_verification(
            _vp(f"touch {marker}; pytest tests/test_x.py"),
            project_dir=tmp_path,
        )
        assert result.status == STATUS_FAILED
        assert result.evidence["schema_issues"]
        assert not marker.exists()

    def test_entry_runs_in_the_project_dir(self, tmp_path):
        (tmp_path / "here.txt").write_text("x")
        result = run_ci_verification(
            _vp("test -f here.txt"), project_dir=tmp_path,
        )
        assert result.status == STATUS_PASSED

    def test_artifact_is_written_when_a_dir_is_given(self, tmp_path):
        artifact = tmp_path / "vp_artifacts"
        run_ci_verification(
            _vp("exit 0"), project_dir=tmp_path, artifact_dir=artifact,
        )
        payload = json.loads(
            (artifact / "VP-901" / "ci_output.json").read_text(encoding="utf-8")
        )
        assert payload["exit_code"] == 0
        assert payload["ci_entry"] == "exit 0"

    def test_verdict_dict_shape(self, tmp_path):
        verdict = run_ci_verification(
            _vp("exit 0"), project_dir=tmp_path,
        ).to_verdict_dict()
        assert set(verdict) == {"status", "reasons", "evidence"}
        assert isinstance(verdict["reasons"], list)
