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
    _referenced_paths,
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
# looks_narrowed — the ``-m`` ambiguity (2026-10-24)
# ---------------------------------------------------------------------------
#
# ``-m`` is the one pytest selector whose spelling collides with another
# program: ``python3 -m pytest`` is the *interpreter's* "run this module"
# flag, and the entry still runs the whole test directory. The old check
# looked at the token alone, so it read the interpreter's ``-m`` as
# pytest's ``--markexpr`` shorthand and rejected a perfectly whole-repo
# entry — VP-019 never ran a single line of the gate because of it.
#
# The discriminator is the token *before* the ``-m``: a python
# interpreter basename (``python`` / ``python3`` / ``python3.11`` /
# ``python3.exe``) means the ``-m`` belongs to the interpreter.
# Everything else — ``-k``, ``--markexpr``, ``::`` node ids, single test
# files — keeps its previous verdict.


class TestLooksNarrowedDashMAmbiguity:
    def test_interpreter_dash_m_module_flag_is_not_narrowed(self):
        assert looks_narrowed("python3 -m pytest backend/tests -q") is None

    def test_interpreter_dash_m_with_venv_path_is_not_narrowed(self):
        assert looks_narrowed(
            "/opt/venv/bin/python3 -m pytest backend/tests -q"
        ) is None

    def test_versioned_interpreter_dash_m_is_not_narrowed(self):
        assert looks_narrowed("python3.11 -m pytest backend/tests") is None

    def test_windows_interpreter_dash_m_is_not_narrowed(self):
        assert looks_narrowed("python3.exe -m pytest backend/tests") is None

    def test_declared_ci_entry_shape_is_not_narrowed(self):
        # The exact shape VP-019 declares: an env prefix, an absolute
        # interpreter path, then ``-m pytest <whole dir>``.
        assert looks_narrowed(
            "CI=true /abs/path/backend/.venv/bin/python3 -m pytest "
            "backend/tests -q"
        ) is None

    def test_pytest_markexpr_after_interpreter_is_still_narrowed(self):
        # The interpreter's ``-m`` is exempt; pytest's own ``-m slow``
        # further down the line is not.
        reason = looks_narrowed("python3 -m pytest -m slow")
        assert reason and "-m" in reason

    def test_pytest_keyword_after_interpreter_is_still_narrowed(self):
        reason = looks_narrowed("python3 -m pytest -k divergence")
        assert reason and "-k" in reason

    def test_pytest_markexpr_equals_form_after_interpreter_is_narrowed(self):
        assert looks_narrowed("python3 -m pytest --markexpr=slow") is not None

    def test_bare_dash_m_without_interpreter_is_still_narrowed(self):
        # No interpreter in front of it → the ``-m`` is pytest's own.
        reason = looks_narrowed("-m slow backend/tests")
        assert reason and "-m" in reason

    def test_dash_m_equals_form_is_still_narrowed(self):
        assert looks_narrowed("pytest backend/tests -m=slow") is not None


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

    def test_interpreter_module_entry_is_not_a_schema_issue(self, tmp_path):
        # ``python3 -m pytest <dir>`` runs the whole directory — the gate
        # must accept it (2026-10-24: the ``-m`` of the interpreter used
        # to be read as pytest's markexpr and rejected the entry).
        (tmp_path / "backend" / "tests").mkdir(parents=True)
        assert validate_vp(
            _vp("python3 -m pytest backend/tests -q"), project_dir=tmp_path,
        ) == []


# ---------------------------------------------------------------------------
# validate_vp — compound command lines (2026-10-29)
# ---------------------------------------------------------------------------
#
# A ``ci_entry`` is a *shell* command line, not a list of arguments. The
# old path reader split it with ``shlex`` and stopped there — so the
# quoted payload of ``bash -c '…'`` came out as ONE token, and the whole
# string was then looked up as a path relative to the repository root.
#
# The consequence was not a nitpick: the gate's own entry
# (``bash scripts/grep_guard.sh && bash scripts/run_tests.sh && bash -c
# 'cd backend && ./.venv/bin/python -m pytest state_machine/tests -q'``)
# was rejected at the schema stage with "path does not exist", the gate
# never ran a single command, and the whole-repo regression evidence was
# silently reduced to zero.
#
# Two things have to be understood for such an entry to be read
# correctly: the payload of a shell's ``-c`` flag is a command *string*
# (split it again), and a path named after a ``cd <dir>`` is relative to
# the directory the command runs in, not to the repository root.


class TestValidateVpCompoundCommands:
    def test_quoted_bash_c_payload_is_split_not_treated_as_one_path(self):
        payload = "cd backend && ./.venv/bin/python -m pytest state_machine/tests -q"
        paths = _referenced_paths(f"bash -c '{payload}'")
        # The quoted command string must not be looked up as one path …
        assert payload not in paths
        # … and the path inside it must be resolved against the ``cd``.
        assert "backend/state_machine/tests" in paths

    def test_cd_prefix_entry_passes_when_paths_exist(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "grep_guard.sh").write_text("#!/bin/sh\n")
        (tmp_path / "scripts" / "run_tests.sh").write_text("#!/bin/sh\n")
        (tmp_path / "backend" / "state_machine" / "tests").mkdir(parents=True)
        venv_bin = tmp_path / "backend" / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("#!/bin/sh\n")
        entry = (
            "bash scripts/grep_guard.sh && bash scripts/run_tests.sh && "
            "bash -c 'cd backend && ./.venv/bin/python -m pytest "
            "state_machine/tests -q'"
        )
        assert validate_vp(_vp(entry), project_dir=tmp_path) == []

    def test_missing_path_after_cd_is_still_an_issue(self, tmp_path):
        # Negative control: the ``cd`` must resolve the path, not turn the
        # existence check into a no-op.
        venv_bin = tmp_path / "backend" / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("#!/bin/sh\n")
        entry = (
            "bash -c 'cd backend && ./.venv/bin/python -m pytest "
            "state_machine/tests -q'"
        )
        issues = validate_vp(_vp(entry), project_dir=tmp_path)
        assert len(issues) == 1
        assert "backend/state_machine/tests" in issues[0].detail
        assert "不存在" in issues[0].detail

    def test_path_after_cd_is_not_resolved_against_the_root(self, tmp_path):
        # The same relative path exists at the repository root — it must
        # still be reported, because the command runs in ``backend/``.
        (tmp_path / "backend").mkdir()
        (tmp_path / "state_machine" / "tests").mkdir(parents=True)
        issues = validate_vp(
            _vp("bash -c 'cd backend && pytest state_machine/tests -q'"),
            project_dir=tmp_path,
        )
        assert any("backend/state_machine/tests" in i.detail for i in issues)

    def test_cd_prefix_applies_to_the_commands_that_follow_it(self, tmp_path):
        (tmp_path / "backend" / "scripts").mkdir(parents=True)
        (tmp_path / "backend" / "scripts" / "run.sh").write_text("#!/bin/sh\n")
        assert validate_vp(
            _vp("cd backend && bash scripts/run.sh"), project_dir=tmp_path,
        ) == []

    def test_flags_urls_and_absolute_paths_are_still_skipped(self, tmp_path):
        entry = (
            "bash -c 'cd backend && pytest -q --junit-xml=out/report.xml "
            "https://example.com/x /opt/bin/tool'"
        )
        assert validate_vp(_vp(entry), project_dir=tmp_path) == []

    def test_run_ci_verification_runs_after_cd_resolution(self, tmp_path):
        backend = tmp_path / "backend"
        backend.mkdir()
        stub = backend / "stub.sh"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        result = run_ci_verification(
            _vp("bash -c 'cd backend && ./stub.sh'"), project_dir=tmp_path,
        )
        assert result.status == STATUS_PASSED
        assert result.evidence["exit_code"] == 0
        assert "schema_issues" not in result.evidence


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
