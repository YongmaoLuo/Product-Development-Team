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
  * the per-phase junit evidence — the reports the gate writes are read
    back into the verdict, and a report that is missing or broken
    leaves the verdict exactly where it was.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_ci_runner as ci_runner  # noqa: E402
from verification_ci_runner import (  # noqa: E402
    DEFAULT_CI_TIMEOUT_SECONDS,
    MAX_JUNIT_FAILURES,
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

    def test_timeout_is_failed_and_marked(self, tmp_path, monkeypatch):
        # The budget is a **floor** for ``full_ci`` (2026-10-31, VP-020):
        # a declared 1 s can no longer kill the gate, because a gate
        # killed by an under-declared budget is not a verdict on the
        # delivery.  So the floor is lowered here to reach the timeout
        # path without waiting 7200 s — the assertion below is about the
        # timeout's shape, not about how long the budget is.
        monkeypatch.setattr(ci_runner, "MIN_FULL_CI_TIMEOUT_SECONDS", 1)
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


# ---------------------------------------------------------------------------
# The budget floor — the declared budget is a floor, not a ceiling
# ---------------------------------------------------------------------------
#
# VP-020 (2026-10-31): the plan declared ``ci_timeout_seconds: 3600`` and
# the whole-repo gate measured 3457.67 s on an idle machine.  The margin
# was 142 s (3.9 %), so the *next* round — on a busier host — was killed
# by the framework's own budget before the gate had finished.  The plan
# cannot be corrected from the outside (``verification_plan_delta`` only
# knows ``add`` / ``obsolete`` / ``replacement_for``), so the runner has
# to hold the floor: a declared budget below the gate's measured cost is
# not honoured as-is.
#
# The floor is a floor, not a new ceiling: a plan that declares *more*
# than the floor keeps its own number.


class TestCiTimeoutFloor:
    def test_full_ci_never_runs_on_less_than_the_measured_gate_cost(self):
        budget = ci_runner.ci_timeout({
            "verification_method": "full_ci",
            "ci_timeout_seconds": 3600,
        })
        assert budget >= ci_runner.MIN_FULL_CI_TIMEOUT_SECONDS
        assert ci_runner.MIN_FULL_CI_TIMEOUT_SECONDS >= 7200

    def test_a_declared_budget_above_the_floor_is_kept(self):
        assert ci_runner.ci_timeout({
            "verification_method": "full_ci",
            "ci_timeout_seconds": 9000,
        }) == 9000

    def test_a_full_ci_vp_without_a_declared_budget_gets_the_default(self):
        assert ci_runner.ci_timeout({"verification_method": "full_ci"}) == (
            ci_runner.DEFAULT_CI_TIMEOUT_SECONDS
        )

    def test_other_verification_methods_keep_their_own_budget(self):
        # The floor belongs to the whole-repo gate: a 120 s api_test is
        # not a whole-repo gate and must not be stretched to two hours.
        assert ci_runner.ci_timeout({
            "verification_method": "api_test",
            "ci_timeout_seconds": 120,
        }) == 120


# ---------------------------------------------------------------------------
# The three states of the gate — "the machine could not decide" is not
# "the code is broken"
# ---------------------------------------------------------------------------
#
# ``scripts/run_tests.sh`` has three exit codes, and the third one is
# the one the framework used to flatten: 0 = green, 1 = a lane went red,
# 3 = the run could not decide (the host could not be measured, or a
# phase called itself inconclusive).  The framework read every non-zero
# exit as "the deliverable is broken", so a busy host produced a repair
# task against a delivery that had not failed.


class TestGateSignals:
    def test_a_green_gate_carries_no_signal(self, tmp_path):
        result = run_ci_verification(_vp("exit 0"), project_dir=tmp_path)
        assert result.status == STATUS_PASSED
        assert "signal" not in result.evidence

    def test_a_red_gate_is_gate_failed(self, tmp_path):
        result = run_ci_verification(_vp("exit 1"), project_dir=tmp_path)
        assert result.status == STATUS_FAILED
        assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_FAILED

    def test_an_inconclusive_gate_is_not_a_code_failure(self, tmp_path):
        result = run_ci_verification(_vp("exit 3"), project_dir=tmp_path)
        assert result.status == STATUS_FAILED
        assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_INCONCLUSIVE

    def test_a_gate_killed_by_its_budget_is_a_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ci_runner, "MIN_FULL_CI_TIMEOUT_SECONDS", 1)
        result = run_ci_verification(
            _vp("sleep 5", ci_timeout_seconds=1), project_dir=tmp_path,
        )
        assert result.evidence["timed_out"] is True
        assert result.evidence["signal"] == ci_runner.SIGNAL_TIMEOUT

    def test_a_gate_that_cannot_be_started_at_all_is_an_infrastructure_problem(
        self, tmp_path, monkeypatch,
    ):
        def _boom(*args, **kwargs):
            raise OSError("no shell for you")

        monkeypatch.setattr(ci_runner, "run_bounded", _boom)
        result = run_ci_verification(_vp("exit 0"), project_dir=tmp_path)
        assert result.status == STATUS_FAILED
        assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_NOT_EXECUTABLE


# ---------------------------------------------------------------------------
# The evidence has to survive the kill
# ---------------------------------------------------------------------------
#
# The second VP-020 round was killed with 4000 characters of stdout
# kept — the *tail*.  The tail was the second phase's progress bar; the
# first phase's summary ("1 failed, 10530 passed … in 822.48 s") had
# already scrolled out of it.  A run that was killed must still be able
# to answer "which lanes finished, and what did they say".


def _gate_output(tmp_path) -> Path:
    """A file that looks like the gate's own summary output."""
    out = tmp_path / "gate_output.txt"
    out.write_text(
        "=== gate summary ===\n"
        "phase 1/3 (parallel):             passed\n"
        "= 1 failed, 10530 passed, 43 skipped, 1 xfailed in 822.48s (0:13:42) =\n"
        "phase 2/3 (e2e + integration):    FAILED (exit 1)\n"
        "gate: FAIL (exit 1) — a phase failed\n",
        encoding="utf-8",
    )
    return out


class TestEvidenceRetention:
    def test_a_killed_gate_still_reports_the_lanes_that_finished(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(ci_runner, "MIN_FULL_CI_TIMEOUT_SECONDS", 1)
        out = _gate_output(tmp_path)
        result = run_ci_verification(
            _vp(f"cat {out}; sleep 30", ci_timeout_seconds=1),
            project_dir=tmp_path,
        )
        assert result.evidence["timed_out"] is True
        summaries = result.evidence["lane_summaries"]
        assert any("phase 1/3" in line for line in summaries), summaries
        assert any("1 failed" in line for line in summaries), summaries
        assert any(line.startswith("gate:") for line in summaries), summaries

    def test_the_head_of_the_output_survives_the_kill(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(ci_runner, "MIN_FULL_CI_TIMEOUT_SECONDS", 1)
        out = _gate_output(tmp_path)
        result = run_ci_verification(
            _vp(f"cat {out}; sleep 30", ci_timeout_seconds=1),
            project_dir=tmp_path,
        )
        assert result.evidence["stdout_head"].startswith("=== gate summary ===")
        assert result.evidence["stdout_tail"]

    def test_the_evidence_names_the_budget_and_says_whether_it_was_exceeded(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(ci_runner, "MIN_FULL_CI_TIMEOUT_SECONDS", 1)
        result = run_ci_verification(
            _vp("sleep 5", ci_timeout_seconds=1), project_dir=tmp_path,
        )
        assert result.evidence["timeout_seconds"] == 1
        assert result.evidence["budget_exceeded"] is True

    def test_a_gate_that_finished_is_not_over_budget(self, tmp_path):
        result = run_ci_verification(_vp("exit 0"), project_dir=tmp_path)
        assert result.evidence["budget_exceeded"] is False
        assert result.evidence["timeout_seconds"] >= (
            ci_runner.MIN_FULL_CI_TIMEOUT_SECONDS
        )

    def test_the_audit_artifact_carries_the_new_evidence(self, tmp_path):
        artifact = tmp_path / "vp_artifacts"
        run_ci_verification(
            _vp("exit 0"), project_dir=tmp_path, artifact_dir=artifact,
        )
        payload = json.loads(
            (artifact / "VP-901" / "ci_output.json").read_text(encoding="utf-8")
        )
        for key in (
            "stdout_head", "lane_summaries", "timeout_seconds", "budget_exceeded",
        ):
            assert key in payload, payload


# ---------------------------------------------------------------------------
# The verdict carries the class — a killed gate is not a broken delivery
# ---------------------------------------------------------------------------


def _full_ci_agent(tmp_path):
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )


def _full_ci_vp(**over) -> dict:
    vp = {
        "id": "VP-020",
        "title": "【全量】Nightly CI：整仓门禁",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "phase_order": 1,
        "ci_entry": "exit 0",
    }
    vp.update(over)
    return vp


class TestFullCiVerdictClassification:
    def test_a_gate_killed_by_its_budget_is_infrastructure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ci_runner, "MIN_FULL_CI_TIMEOUT_SECONDS", 1)
        verdict = _full_ci_agent(tmp_path)._run_full_ci_vp(
            _full_ci_vp(ci_entry="sleep 5", ci_timeout_seconds=1),
        )
        assert verdict["status"] == "FAILED"
        assert verdict["failure_class"] == "infra", verdict
        assert verdict["failure_class_source"] == "deterministic", verdict

    def test_an_inconclusive_gate_is_infrastructure(self, tmp_path):
        verdict = _full_ci_agent(tmp_path)._run_full_ci_vp(
            _full_ci_vp(ci_entry="exit 3"),
        )
        assert verdict["status"] == "FAILED"
        assert verdict["failure_class"] == "infra", verdict
        assert verdict["failure_class_source"] == "deterministic", verdict

    def test_a_red_gate_is_a_deliverable_defect(self, tmp_path):
        verdict = _full_ci_agent(tmp_path)._run_full_ci_vp(
            _full_ci_vp(ci_entry="exit 1"),
        )
        assert verdict["status"] == "FAILED"
        assert verdict["failure_class"] == "deliverable_defect", verdict
        assert verdict["failure_class_source"] == "deterministic", verdict

    def test_a_green_gate_carries_no_failure_class(self, tmp_path):
        verdict = _full_ci_agent(tmp_path)._run_full_ci_vp(_full_ci_vp())
        assert verdict["status"] == "PASSED"
        assert set(verdict) == {"status", "reasons", "evidence"}
# ---------------------------------------------------------------------------
# The per-phase junit evidence
# ---------------------------------------------------------------------------
#
# The gate script runs three phases back to back and the verdict keeps
# only the tail of the whole run's output — so a failure in phase 1 is
# gone from the evidence by the time phase 3 ends.  The junit reports
# are what makes the failure locatable again.
#
# The rule this file pins: **the reports are evidence, never a verdict.**
# They are read back, they are summarised, and a report that is missing
# or broken changes nothing about PASSED / FAILED — because that verdict
# is the exit code and nothing else.

#: A fake CI entry.  It publishes the prepared reports into the
#: directory the runner named through ``PDT_GATE_JUNIT_DIR`` — the very
#: variable the real gate script reads — so the test exercises the
#: wiring the production path uses rather than reaching into the
#: reader directly.
#:
#: The exit code is the one the test asked for; when the environment
#: variable is missing the entry exits 99 instead, so a test can tell
#: "the gate ran and failed" from "the gate never learned where to
#: write".
_FAKE_GATE_SOURCE = '''\
import os
import shutil
import sys
from pathlib import Path

target = os.environ.get("PDT_GATE_JUNIT_DIR")
if not target:
    sys.exit(99)
destination = Path(target)
destination.mkdir(parents=True, exist_ok=True)
for report in sorted((Path(__file__).parent / "reports").glob("*.xml")):
    shutil.copyfile(report, destination / report.name)
sys.exit(int(sys.argv[1]))
'''

#: One phase with two failures and one skipped case, in the shape pytest
#: writes.
_PHASE_ONE_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" errors="0" failures="2" skipped="1" tests="3" \
time="0.123" timestamp="2026-10-31T00:00:00" hostname="gate">
    <testcase classname="tests.unit.test_alpha" name="test_first" time="0.001">
      <failure message="assert 1 == 2">def test_first():
&gt;       assert 1 == 2
E       assert 1 == 2</failure>
    </testcase>
    <testcase classname="tests.unit.test_alpha" name="test_second" time="0.002">
      <failure message="ValueError: boom">def test_second():
E       ValueError: boom</failure>
    </testcase>
    <testcase classname="tests.unit.test_alpha" name="test_skipped" time="0.000">
      <skipped type="pytest.skip" message="not today" />
    </testcase>
  </testsuite>
</testsuites>
"""

#: A second phase, carrying a collection error rather than a failure.
_PHASE_TWO_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" errors="1" failures="0" skipped="0" tests="1" \
time="0.500" timestamp="2026-10-31T00:00:01" hostname="gate">
    <testcase classname="tests.unit.test_beta" name="test_collect" time="0.500">
      <error message="collection failure">ImportError: no module named beta</error>
    </testcase>
  </testsuite>
</testsuites>
"""


def _fake_gate(tmp_path: Path, reports: dict, exit_code: int = 1) -> str:
    """Write the fake entry and return the ``ci_entry`` that runs it."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    for name, body in reports.items():
        (reports_dir / name).write_text(body, encoding="utf-8")
    gate = tmp_path / "fake_gate.py"
    gate.write_text(_FAKE_GATE_SOURCE, encoding="utf-8")
    return f'"{sys.executable}" "{gate}" {exit_code}'


def _junit_xml(failures: int) -> str:
    """A phase report with ``failures`` failing cases."""
    cases = "".join(
        f'<testcase classname="tests.unit.test_many" name="test_{index}" '
        f'time="0.001"><failure message="assert {index} == {index + 1}">'
        f"E       assert {index} == {index + 1}</failure></testcase>"
        for index in range(failures)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuites name="pytest tests">\n'
        f'  <testsuite name="pytest" errors="0" failures="{failures}" '
        f'skipped="0" tests="{failures}" time="1.000" '
        'timestamp="2026-10-31T00:00:00" hostname="gate">\n'
        f"    {cases}\n"
        "  </testsuite>\n"
        "</testsuites>\n"
    )


class TestJunitEvidence:
    def test_the_reports_of_every_phase_are_read_back(self, tmp_path):
        entry = _fake_gate(
            tmp_path,
            {"phase-1.xml": _PHASE_ONE_XML, "phase-2.xml": _PHASE_TWO_XML},
        )
        result = run_ci_verification(
            _vp(entry), project_dir=tmp_path, artifact_dir=tmp_path / "arts",
        )

        # The entry ran with the exit code it was given (99 would mean
        # it never learned where to write).
        assert result.evidence["exit_code"] == 1

        assert result.evidence["junit_phases"] == [
            {"phase": 1, "tests": 3, "failures": 2, "errors": 0, "skipped": 1},
            {"phase": 2, "tests": 1, "failures": 0, "errors": 1, "skipped": 0},
        ]

        failures = result.evidence["junit_failures"]
        assert [f["node_id"] for f in failures] == [
            "tests/unit/test_alpha.py::test_first",
            "tests/unit/test_alpha.py::test_second",
            "tests/unit/test_beta.py::test_collect",
        ]
        assert [f["phase"] for f in failures] == [1, 1, 2]
        assert [f["kind"] for f in failures] == ["failure", "failure", "error"]
        assert failures[0]["first_line"] == "assert 1 == 2"
        assert failures[1]["first_line"] == "ValueError: boom"
        assert failures[2]["first_line"] == "collection failure"

    def test_the_verdict_is_still_the_exit_code(self, tmp_path):
        entry = _fake_gate(tmp_path, {"phase-1.xml": _PHASE_ONE_XML})
        result = run_ci_verification(
            _vp(entry), project_dir=tmp_path, artifact_dir=tmp_path / "arts",
        )
        assert result.status == STATUS_FAILED
        assert result.evidence["junit_failures"]

    def test_a_passing_gate_with_reports_is_still_passed(self, tmp_path):
        entry = _fake_gate(
            tmp_path, {"phase-1.xml": _PHASE_ONE_XML}, exit_code=0,
        )
        result = run_ci_verification(
            _vp(entry), project_dir=tmp_path, artifact_dir=tmp_path / "arts",
        )
        assert result.status == STATUS_PASSED
        # The reports are still read — a passing gate's reports say how
        # much it ran.
        assert result.evidence["junit_phases"][0]["tests"] == 3

    def test_no_reports_leaves_the_evidence_empty_and_the_verdict_alone(
        self, tmp_path,
    ):
        result = run_ci_verification(
            _vp("exit 1"), project_dir=tmp_path, artifact_dir=tmp_path / "arts",
        )
        assert result.status == STATUS_FAILED
        assert result.evidence["junit_phases"] == []
        assert result.evidence["junit_failures"] == []
        assert "junit_error" not in result.evidence

    def test_a_broken_report_is_recorded_and_never_raised(self, tmp_path):
        entry = _fake_gate(
            tmp_path, {"phase-1.xml": "<testsuites><not-closed>"},
        )
        result = run_ci_verification(
            _vp(entry), project_dir=tmp_path, artifact_dir=tmp_path / "arts",
        )
        assert result.status == STATUS_FAILED
        assert result.evidence["exit_code"] == 1
        assert result.evidence["junit_error"]
        assert "phase-1.xml" in result.evidence["junit_error"]
        assert result.evidence["junit_failures"] == []

    def test_more_failures_than_the_ceiling_are_truncated(self, tmp_path):
        entry = _fake_gate(
            tmp_path,
            {"phase-1.xml": _junit_xml(MAX_JUNIT_FAILURES + 10)},
        )
        result = run_ci_verification(
            _vp(entry), project_dir=tmp_path, artifact_dir=tmp_path / "arts",
        )
        assert result.status == STATUS_FAILED
        assert len(result.evidence["junit_failures"]) == MAX_JUNIT_FAILURES
        assert result.evidence["junit_failures_truncated"] is True
        # The count of what was left out is still reported.
        assert result.evidence["junit_phases"][0]["failures"] == (
            MAX_JUNIT_FAILURES + 10
        )

    def test_the_truncation_flag_is_absent_below_the_ceiling(self, tmp_path):
        entry = _fake_gate(tmp_path, {"phase-1.xml": _junit_xml(3)})
        result = run_ci_verification(
            _vp(entry), project_dir=tmp_path, artifact_dir=tmp_path / "arts",
        )
        assert len(result.evidence["junit_failures"]) == 3
        assert "junit_failures_truncated" not in result.evidence

    def test_the_reports_land_next_to_the_audit_copy(self, tmp_path):
        entry = _fake_gate(tmp_path, {"phase-1.xml": _PHASE_ONE_XML})
        artifact = tmp_path / "arts"
        run_ci_verification(
            _vp(entry), project_dir=tmp_path, artifact_dir=artifact,
        )
        assert (artifact / "VP-901" / "phase-1.xml").exists()
        assert (artifact / "VP-901" / "ci_output.json").exists()

    def test_the_audit_copy_carries_the_junit_evidence(self, tmp_path):
        entry = _fake_gate(tmp_path, {"phase-1.xml": _PHASE_ONE_XML})
        artifact = tmp_path / "arts"
        run_ci_verification(
            _vp(entry), project_dir=tmp_path, artifact_dir=artifact,
        )
        payload = json.loads(
            (artifact / "VP-901" / "ci_output.json").read_text(encoding="utf-8")
        )
        assert payload["junit_phases"][0]["failures"] == 2
        assert payload["junit_failures"][0]["node_id"] == (
            "tests/unit/test_alpha.py::test_first"
        )

    def test_without_an_artifact_dir_there_is_nowhere_to_write(self, tmp_path):
        result = run_ci_verification(_vp("exit 0"), project_dir=tmp_path)
        assert result.status == STATUS_PASSED
        assert result.evidence["junit_phases"] == []
        assert result.evidence["junit_failures"] == []
