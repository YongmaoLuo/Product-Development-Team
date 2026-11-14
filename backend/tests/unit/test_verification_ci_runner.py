"""Unit tests for the full CI gate runner (2026-09-18 Phase 2 gate).

Covers:
  * ``looks_narrowed`` — the anti-degeneration check that keeps
    ``ci_entry`` from decaying back into a general-purpose
    ``test_command`` (single test file / ``-k`` / ``::`` / cargo
    selectors).
  * ``validate_vp`` — required field, narrowing rejection, and the
    repo-path existence check.
  * ``validate_vp`` — a path the entry **creates** (``ln -s … backend/.venv``)
    is not an input it depends on, so it is exempt from that check; a
    path the entry only **reads** is not.
  * ``run_ci_verification`` — exit 0 → PASSED; non-zero / timeout /
    missing entry → FAILED with the reason; the audit artifact.
  * the per-phase junit evidence — the reports the gate writes are read
    back into the verdict, and a report that is missing or broken
    leaves the verdict exactly where it was.
"""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
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
    _created_paths,
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


#: VP-022 冻结入口的**形状**（逐字取自
#: ``plans/<plan-id>/verification_plan.json`` 里那条
#: ``full_ci`` 验证点的 ``ci_entry``）。
#:
#: 绝对路径换成了 ``/opt/shared/venv``：入口里那条路径**本机专属**，而门禁
#: 对绝对路径不做存在性检查（``_looks_like_path`` 主动跳过）—— 换掉它不
#: 影响任何一条断言，留在测试里反而会把一台机器的目录结构写进仓里。
VP022_FROZEN_ENTRY = (
    "(test -e backend/.venv || ln -sfn /opt/shared/venv backend/.venv) && "
    "bash scripts/run_tests.sh"
)


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
        assert validate_vp(
            _vp(entry), project_dir=tmp_path, require_single_entry=False,
        ) == []

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
        issues = validate_vp(
            _vp(entry), project_dir=tmp_path, require_single_entry=False,
        )
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
            require_single_entry=False,
        )
        assert any("backend/state_machine/tests" in i.detail for i in issues)

    def test_cd_prefix_applies_to_the_commands_that_follow_it(self, tmp_path):
        (tmp_path / "backend" / "scripts").mkdir(parents=True)
        (tmp_path / "backend" / "scripts" / "run.sh").write_text("#!/bin/sh\n")
        assert validate_vp(
            _vp("cd backend && bash scripts/run.sh"), project_dir=tmp_path,
            require_single_entry=False,
        ) == []

    def test_flags_urls_and_absolute_paths_are_still_skipped(self, tmp_path):
        entry = (
            "bash -c 'cd backend && pytest -q --junit-xml=out/report.xml "
            "https://example.com/x /opt/bin/tool'"
        )
        assert validate_vp(
            _vp(entry), project_dir=tmp_path, require_single_entry=False,
        ) == []

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
# validate_vp — shell 分组括号与入口自建路径（2026-11-05）
# ---------------------------------------------------------------------------
#
# 一条真实的整仓门禁入口长这样：
#
#     (test -e backend/.venv || ln -sfn <别处的 venv> backend/.venv) \
#         && CI=true bash scripts/run_tests.sh
#
# 两个坑叠在一起，把它挡在了 schema 预检上，门禁一行都没跑：
#
# 1. **分组括号没人剥。** ``(test`` 与 ``backend/.venv)`` 都是带括号
#    的 token，``backend/.venv)`` 于是被当成一个独立的仓库相对路径去
#    查存在性 —— 一条纯解析伪影的 issue。前导的 ``(`` 同理让
#    ``(cd backend && …)`` 里的 ``cd`` 不被认出来，路径解析基准错了。
# 2. **入口自己建的路径被当成"引用的路径"。** 入口用 ``ln -sfn`` 在
#    工作树里自建 ``backend/.venv``（新 clone 的树里没有它），框架却
#    把"入口要创建这个路径"读成"入口要读取这个路径"，于是一条完全
#    可跑的入口被判成不可运行。
#
# 豁免只给**生产者位置**：入口自己 ``ln`` / ``mkdir`` / ``touch`` /
# ``cp`` / ``mv`` 出来的那一个路径不查存在性；被**消费**的路径
# （``bash scripts/missing.sh``、``pytest state_machine/tests``）照旧
# 必须存在 —— 负控不放宽。


class TestValidateVpShellGroupingAndSelfCreatedPaths:
    def test_grouping_parens_are_stripped_from_path_tokens(self):
        paths = _referenced_paths(
            "(test -e backend/.venv || true) && bash scripts/run_tests.sh",
        )
        # 括号不是路径的一部分 …
        assert "backend/.venv)" not in paths
        # … 剥掉之后它就是那条路径本身。
        assert "backend/.venv" in paths
        assert "scripts/run_tests.sh" in paths

        # 贴着右括号的写法也要剥 —— 那正是报告里那条伪影 issue 的来源。
        attached = _referenced_paths("(ln -sfn /tmp/venv backend/.venv)")
        assert "backend/.venv)" not in attached
        assert "backend/.venv" in attached

    def test_entry_that_creates_a_path_it_checks_passes_schema(self, tmp_path):
        # 这就是 VP-015 的入口形态：入口在工作树里自建 backend/.venv，
        # 再跑整仓门禁。它必须被判成**可运行**。
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run_tests.sh").write_text("#!/bin/sh\n")
        entry = (
            "(test -e backend/.venv || ln -sfn /tmp/venv backend/.venv) && "
            "CI=true bash scripts/run_tests.sh"
        )
        # 执行期形态：历史计划的复合入口照旧要能跑，所以这里关掉
        # 「必须是对适配脚本的一次调用」那道**规划期**契约
        # （``require_single_entry=False``，见 ``validate_vp`` 的 docstring）。
        assert validate_vp(
            _vp(entry), project_dir=tmp_path, require_single_entry=False,
        ) == []

    def test_the_vp022_entry_against_the_real_worktree_has_no_issue(self):
        """U2 —— 同一条入口，project_dir = **真实工作树**：零 issue。

        这是 L1（路径读取层）的收口证据：VP-022 那条入口在真实树上跑之前
        就被判不可运行，代价是整整一轮门禁。真实树里 ``scripts/run_tests.sh``
        存在、``backend/.venv`` 由入口自己建 —— 零 issue 才是正确答案。
        """
        repo_root = Path(__file__).resolve().parents[3]
        assert (repo_root / "scripts" / "run_tests.sh").is_file(), (
            "the repo must still ship the gate script the entry names"
        )
        assert validate_vp(
            _vp(VP022_FROZEN_ENTRY),
            project_dir=repo_root,
            require_single_entry=False,
        ) == []

    def test_created_path_exemption_keeps_the_existence_check_for_consumed_paths(
        self, tmp_path,
    ):
        # 负控：豁免只给生产者位置。被消费的路径照旧必须存在。
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run_tests.sh").write_text("#!/bin/sh\n")
        entry = (
            "(test -e backend/.venv || ln -sfn /tmp/venv backend/.venv) && "
            "CI=true bash scripts/run_tests.sh && bash scripts/missing.sh"
        )
        issues = validate_vp(
            _vp(entry), project_dir=tmp_path, require_single_entry=False,
        )
        assert any("scripts/missing.sh" in i.detail for i in issues), issues

        # ``cd`` 之后的路径也一样：解析基准对了，存在性检查不能松。
        venv_bin = tmp_path / "backend" / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("#!/bin/sh\n")
        issues = validate_vp(
            _vp("cd backend && ./.venv/bin/python -m pytest state_machine/tests -q"),
            project_dir=tmp_path,
            require_single_entry=False,
        )
        assert any("backend/state_machine/tests" in i.detail for i in issues), issues

    def test_leading_paren_does_not_hide_cd(self, tmp_path):
        # 前导的 ``(`` 不能把 ``cd`` 藏起来 —— 括号里的路径仍然相对
        # 于 ``cd`` 之后的目录解析。
        venv_bin = tmp_path / "backend" / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("#!/bin/sh\n")
        (tmp_path / "backend" / "state_machine" / "tests").mkdir(parents=True)
        assert validate_vp(
            _vp("(cd backend && ./.venv/bin/python -m pytest state_machine/tests -q)"),
            project_dir=tmp_path,
            require_single_entry=False,
        ) == []

    def test_a_missing_path_inside_a_group_is_still_an_issue(self, tmp_path):
        # 负控：括号不豁免任何存在性检查。
        (tmp_path / "scripts").mkdir()
        issues = validate_vp(
            _vp("(bash scripts/missing.sh)"), project_dir=tmp_path,
            require_single_entry=False,
        )
        assert any("scripts/missing.sh" in i.detail for i in issues), issues

    def test_only_the_producer_position_is_exempt(self, tmp_path):
        # ``mkdir`` / ``touch`` 创建它们的操作数；``cp`` / ``mv`` 创建的是
        # 目标，而**源**是被消费的 —— 源照旧必须存在。
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run_tests.sh").write_text("#!/bin/sh\n")
        assert validate_vp(
            _vp(
                "mkdir -p backend/.venv && touch backend/.venv/marker "
                "&& bash scripts/run_tests.sh",
            ),
            project_dir=tmp_path,
            require_single_entry=False,
        ) == []

        issues = validate_vp(
            _vp(
                "cp scripts/missing.txt scripts/out.sh "
                "&& mv scripts/other.txt scripts/out2.sh "
                "&& bash scripts/run_tests.sh",
            ),
            project_dir=tmp_path,
            require_single_entry=False,
        )
        assert any("scripts/missing.txt" in i.detail for i in issues), issues
        assert any("scripts/other.txt" in i.detail for i in issues), issues
        # 目标位置是生产者 —— 不报。
        assert not any("scripts/out.sh" in i.detail for i in issues), issues
        assert not any("scripts/out2.sh" in i.detail for i in issues), issues


# ---------------------------------------------------------------------------
# 规划期的入口形态契约（2026-11-06）
# ---------------------------------------------------------------------------
#
# 2026-11-05 的 arch 决策点 2：整仓门禁必须由**项目适配脚本**执行，一条
# ``ci_entry`` 是对那条脚本的**一次调用**，不是内联复合命令。于是
# ``validate_vp`` 有两种形态：
#
# * ``require_single_entry=True``（缺省）—— **规划期**的契约。一条内联复合
#   命令在计划期就该被退回重写，而不是写进计划、被整份复用、每一轮都在执行期
#   第一次被看见。
# * ``require_single_entry=False`` —— **执行期**的契约。历史计划里的复合入口
#   照旧要能跑，所以执行期不做形态检查（VP-022 冻结的那条入口就是这种）。
#
# 规划期的那条拒绝必须留下**机器写的身份**：否则「内联复合命令」这一类缺陷的
# 身份是 ``schema:unknown`` —— 所有形态违规共用一个身份，指纹分不出缺陷，
# 循环又会把「同一类缺陷」和「同一个缺陷」混成一谈。


#: VP-022 冻结入口的形状见文件头的 ``VP022_FROZEN_ENTRY``。


class TestPlanTimeEntryShape:
    """规划期：内联复合入口被退回；执行期：同一条入口照旧能跑。"""

    def test_a_compound_entry_is_an_issue_at_plan_time(self):
        issues = validate_vp(_vp(VP022_FROZEN_ENTRY))

        assert [i.code for i in issues] == ["ci_entry_compound"], (
            f"the plan-time shape contract must reject the compound entry; "
            f"got {[i.to_dict() for i in issues]!r}"
        )

    def test_the_rejection_carries_a_machine_written_identity(self):
        issues = validate_vp(_vp(VP022_FROZEN_ENTRY))

        assert issues[0].failure_id() == "schema:ci_entry_compound:&&,||,(,)", (
            f"the identity must name the rule and its subject — a generic "
            f"'schema:unknown' makes every shape violation one defect; got "
            f"{issues[0].failure_id()!r}"
        )

    def test_two_different_compound_entries_are_two_different_defects(self):
        first = validate_vp(_vp(VP022_FROZEN_ENTRY))[0].failure_id()
        second = validate_vp(
            _vp("bash scripts/ci_gate.sh && bash scripts/ci_gate.sh"),
        )[0].failure_id()

        assert first != second, (
            "two entries with different operators must not share one "
            "failure identity"
        )

    def test_the_adapter_entry_is_accepted_at_plan_time(self, tmp_path):
        # 闸门不能报它自己教的那条入口 —— 否则重生成回路是死循环。
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "ci_gate.sh").write_text("#!/bin/sh\n")
        assert validate_vp(
            _vp("bash scripts/ci_gate.sh"), project_dir=tmp_path,
        ) == []

    def test_the_same_entry_is_accepted_at_execution_time(self, tmp_path):
        # 执行期不做形态检查：历史计划里的复合入口照旧要能跑。
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run_tests.sh").write_text("#!/bin/sh\n")
        assert validate_vp(
            _vp(VP022_FROZEN_ENTRY),
            project_dir=tmp_path,
            require_single_entry=False,
        ) == []


# ---------------------------------------------------------------------------
# 失败身份：认路径，不认写法
# ---------------------------------------------------------------------------


class TestFailureIdentityIsCanonical:
    """同一个缺陷，换一种写法，还是同一个缺陷（2026-11-06）。

    失败身份取自机器写的失败集合，集合里的每个 id 取自「哪条路径不存在」。
    同一个文件写成 ``./scripts/missing.sh`` 和 ``scripts/missing.sh`` 是**同一个
    缺陷** —— 报告换一种写法，指纹就换一次，循环又判成"根因演进"，于是又拿
    旧根因的方案再打一轮。

    2026-11-05 的 VP-015 正是这样烧掉一轮的：那一轮报告的路径带着一条多余的
    右括号，下一轮不带 ⇒ 机器写的集合不同 ⇒ 指纹不同 ⇒ "根因演进"。解析器
    修好了括号，**写法**这一层还没修：身份必须认路径本身。
    """

    def test_the_same_missing_file_spelled_two_ways_is_one_issue(self, tmp_path):
        (tmp_path / "scripts").mkdir()

        dotted = validate_vp(
            _vp("bash ./scripts/missing.sh"), project_dir=tmp_path,
        )
        plain = validate_vp(
            _vp("bash scripts/missing.sh"), project_dir=tmp_path,
        )

        assert [issue.failure_id() for issue in dotted] == [
            issue.failure_id() for issue in plain
        ]
        assert dotted[0].failure_id() == (
            "schema:ci_entry_path_missing:scripts/missing.sh"
        )

    def test_the_reported_path_is_the_canonical_one(self, tmp_path):
        (tmp_path / "scripts").mkdir()

        issues = validate_vp(
            _vp("bash ./scripts/missing.sh"), project_dir=tmp_path,
        )

        assert issues[0].subject == "scripts/missing.sh"
        assert "scripts/missing.sh" in issues[0].detail


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
# An INCONCLUSIVE verdict has to say *why* it is inconclusive
# ---------------------------------------------------------------------------
#
# The runner grades a gate that could not decide as INCONCLUSIVE, and it
# has always said so in the verdict.  What it did **not** say was the
# machine-readable fact: the evidence carried no ``signal``, and the
# triage layer reads a missing signal as "nothing was determined about
# the deliverable" — which it files as a ``deliverable_defect``, the
# class that becomes a repair task.  So a gate that ran out of its
# budget sent the repair loop after a delivery nobody had proven
# defective.
#
# What is pinned here:
#
# * every INCONCLUSIVE path writes ``gate_inconclusive`` into the
#   evidence — the gate ran out of its budget, the gate said it could
#   not decide, the host was not measurable;
# * that evidence, fed to the triage layer, is an infrastructure
#   problem and **not** a deliverable defect;
# * the FAILED branch keeps every signal it had — the negative control.


#: A gate that ran out of its own budget, in the real gate's words.
_GATE_OUT_OF_BUDGET = """
echo '=== gate summary ==='
echo 'phase 1/3 (parallel):             INCONCLUSIVE (the gate budget was exhausted)'
echo 'host measurability:               OK (sleep(0.05)=0.051s, loadavg=3.10)'
echo 'gate budget: 7200s'
echo 'gate elapsed: 7200s'
echo 'gate budget exhausted in: 1/3 (parallel)'
echo 'gate: INCONCLUSIVE (exit 3)'
exit 3
"""

#: A gate that could not decide without naming a phase — the host was
#: not measurable, so the real gate prints no "budget exhausted" line.
_GATE_HOST_UNMEASURABLE = """
echo '=== gate summary ==='
echo 'phase 1/3 (parallel):             FAILED (exit 1)'
echo 'host measurability:               the host cannot measure wall-clock (sleep(0.05)=1.20s, loadavg=31.00)'
echo 'gate budget: 7200s'
echo 'gate elapsed: 300s'
echo 'gate: INCONCLUSIVE (exit 3)'
exit 3
"""


def _gate_script(tmp_path: Path, body: str) -> str:
    """Write a gate script and return the ``ci_entry`` that runs it."""
    gate = tmp_path / "gate.sh"
    gate.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    gate.chmod(0o755)
    return f"bash {gate}"


class TestInconclusiveSignal:
    def test_a_gate_that_ran_out_of_its_budget_carries_the_signal(
        self, tmp_path,
    ):
        """The gate stopped at its own budget — that is the machine's fault.

        Without a signal the triage layer has nothing to classify, and a
        missing signal lands on ``deliverable_defect``: a repair task
        against a delivery that was never judged.
        """
        result = run_ci_verification(
            _vp(_gate_script(tmp_path, _GATE_OUT_OF_BUDGET)),
            project_dir=tmp_path,
        )

        assert result.status == ci_runner.STATUS_INCONCLUSIVE, result.reasons
        assert result.evidence["signal"] == "gate_inconclusive", result.evidence
        assert result.evidence["signal"] == ci_runner.SIGNAL_GATE_INCONCLUSIVE

    def test_a_gate_whose_host_cannot_be_measured_carries_the_signal(
        self, tmp_path,
    ):
        """The host was not measurable — still the machine's fault."""
        result = run_ci_verification(
            _vp(_gate_script(tmp_path, _GATE_HOST_UNMEASURABLE)),
            project_dir=tmp_path,
        )

        assert result.status == ci_runner.STATUS_INCONCLUSIVE, result.reasons
        assert result.evidence["signal"] == "gate_inconclusive", result.evidence

    def test_the_signal_reaches_the_triage_layer_as_infrastructure(
        self, tmp_path,
    ):
        """The chain: the runner's evidence → the triage layer's class.

        This is the whole point of the signal.  ``gate_inconclusive`` is
        an infrastructure problem; a ``deliverable_defect`` would send
        the repair loop to fix a defect that nobody found.
        """
        from verification.failure_triage import (
            DELIVERABLE_DEFECT,
            INFRA,
            classify_deterministic,
        )

        result = run_ci_verification(
            _vp(_gate_script(tmp_path, _GATE_OUT_OF_BUDGET)),
            project_dir=tmp_path,
        )

        failure_class = classify_deterministic(
            result.evidence.get("signal"), str(result.evidence.get("error") or ""),
        )
        assert failure_class == INFRA, (
            f"an inconclusive gate was filed as {failure_class!r}"
        )
        assert failure_class != DELIVERABLE_DEFECT

    def test_the_failed_branches_keep_their_own_signals(self, tmp_path, monkeypatch):
        """The negative control: the FAILED branch is untouched.

        Every way the runner can say "this failed" keeps the signal it
        had — a timeout is a timeout, a gate that could not be started
        is a gate that could not be started, a red lane is a red lane,
        and an exit-3 that declared nothing is still the gate's own
        ``gate_inconclusive``.
        """
        monkeypatch.setattr(ci_runner, "MIN_FULL_CI_TIMEOUT_SECONDS", 1)

        red = run_ci_verification(_vp("exit 1"), project_dir=tmp_path)
        assert red.status == STATUS_FAILED
        assert red.evidence["signal"] == ci_runner.SIGNAL_GATE_FAILED

        killed = run_ci_verification(
            _vp("sleep 5", ci_timeout_seconds=1), project_dir=tmp_path,
        )
        assert killed.status == STATUS_FAILED
        assert killed.evidence["signal"] == ci_runner.SIGNAL_TIMEOUT

        silent_three = run_ci_verification(_vp("exit 3"), project_dir=tmp_path)
        assert silent_three.status == STATUS_FAILED
        assert silent_three.evidence["signal"] == ci_runner.SIGNAL_GATE_INCONCLUSIVE

        def _boom(*args, **kwargs):
            raise OSError("no shell for you")

        monkeypatch.setattr(ci_runner, "run_bounded", _boom)
        not_executable = run_ci_verification(
            _vp("exit 0"), project_dir=tmp_path,
        )
        assert not_executable.status == STATUS_FAILED
        assert not_executable.evidence["signal"] == (
            ci_runner.SIGNAL_GATE_NOT_EXECUTABLE
        )


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

    def test_a_gate_that_could_not_decide_is_infrastructure(self, tmp_path):
        """The chain, end to end: runner → evidence → triage layer.

        A gate that ran out of its budget is INCONCLUSIVE — the verdict
        says so — but the *class* comes from the evidence's signal.  With
        no signal the verdict carried no class at all, and a verdict
        without a class is a deliverable defect: a repair task against a
        delivery nobody had proven defective.
        """
        verdict = _full_ci_agent(tmp_path)._run_full_ci_vp(
            _full_ci_vp(ci_entry=_gate_script(tmp_path, _GATE_OUT_OF_BUDGET)),
        )
        assert verdict["status"] == "INCONCLUSIVE", verdict
        assert verdict["evidence"]["signal"] == "gate_inconclusive", verdict
        assert verdict["failure_class"] == "infra", verdict
        assert verdict["failure_class"] != "deliverable_defect", verdict
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
        # The shape the executor reads: the status, the reasons, the
        # evidence -- and the gate's own list of the cases that failed,
        # which the report entry and the failure fingerprint read.
        # A green gate has no failure class and no failing case.
        assert set(verdict) == {
            "status", "reasons", "evidence", "failed_assertions",
        }
        assert verdict["failed_assertions"] == [], verdict
        assert "failure_class" not in verdict, verdict
        assert "failure_class_source" not in verdict, verdict


# ---------------------------------------------------------------------------
# 出处：结论说得出来，而且**留在不会丢的地方**
# ---------------------------------------------------------------------------
#
# 2026-11-06 的那轮（round 4）的证据：``full_ci_executed`` 事件里没有
# ``verifier_source``，``vp_artifacts/VP-015/`` 也不存在 —— 于是"这次判定
# 跑的是哪份代码"**没有任何磁盘证据**。这正是本次事故里最贵的一层：框架
# 答不出来，于是同一件事重复了一轮又一轮（根因报告 F2）。
#
# 两条判据：
#
# 1. **加载器坏掉不许抹掉 runner 自报的出处** —— loader 知道的是"这份是
#    **怎么**被选中的"（resolution / fallback_reason），runner 知道的是
#    "**我就是**哪个文件"（module_path / sha256 / git_head）。loader 知道得
#    **更少**的时候，它那份空值不许盖掉 runner 已经答出来的答案。
# 2. **出处要落在计划的持久审计里** —— 轮次日志是每轮一个文件、还可能没
#    开着；``logs/audit_events.jsonl`` 才是那件不会丢的东西。
class TestVerifierProvenanceSurvivesTheRound:
    """出处随结论走，而且留在不会丢的地方。"""

    def test_a_broken_loader_does_not_erase_who_judged(
        self, tmp_path, monkeypatch,
    ):
        """加载器自己坏了的时候，结论**仍然**说得出"谁在判"。

        loader 的出处直接盖住 runner 自报的出处 —— 于是加载器一坏，结论里的
        ``module_path`` / ``sha256`` / ``git_head`` 全被抹成空串，"谁在判"
        就答不出来了。而这正是本次事故里最贵的那一层：答不出来，于是重复
        失败。
        """
        import verification_ci_runner_loader as loader

        def _boom(project_dir=None):
            raise RuntimeError("loader is not importable")

        monkeypatch.setattr(loader, "load_ci_runner", _boom)

        verdict = _full_ci_agent(tmp_path)._run_full_ci_vp(_full_ci_vp())
        source = verdict["evidence"]["verifier_source"]

        # the runner still says which file it is ...
        assert source["module_path"] == str(
            Path(ci_runner.__file__).resolve()
        ), source
        assert source["sha256"], source
        assert source["git_head"], source
        # ... and the loader's own failure is still on the record
        assert source["fallback_reason"], source
        assert "loader is not importable" in source["fallback_reason"], source

    def test_the_provenance_reaches_the_durable_audit(self, tmp_path):
        """出处不只进轮次日志 —— 轮次日志没开时它就丢了。

        round 4 的 ``full_ci_executed`` 事件里没有 ``verifier_source``，而
        ``vp_artifacts/VP-015/`` 也不存在：那轮**一条**关于"谁在判"的磁盘
        证据都没留下。计划的持久审计（``logs/audit_events.jsonl``）不依赖
        轮次日志开着，所以它才是那件不会丢的东西。
        """
        verdict = _full_ci_agent(tmp_path)._run_full_ci_vp(_full_ci_vp())

        audit = tmp_path / "plan" / "logs" / "audit_events.jsonl"
        assert audit.is_file(), (
            "the provenance must not live only in the round log"
        )
        entries = [
            json.loads(line)
            for line in audit.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        recorded = [
            entry for entry in entries
            if entry.get("event_type") == "verifier_provenance_recorded"
        ]
        assert recorded, entries
        source = recorded[0]["data"]["verifier_source"]
        assert source == verdict["evidence"]["verifier_source"], recorded
        assert source["module_path"], source
        assert source["resolution"] in ("project", "harness"), source


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


# ---------------------------------------------------------------------------
# 出处随结论走（2026-11-05，F2）
# ---------------------------------------------------------------------------
#
# 2026-11-05 的 VP-015 失败了两轮，两轮的 detail 逐字节相同，而判定用的
# 评分器早就修好了 —— 差别只在**谁在判**。框架当时答不了这个问题，于是
# "修复没部署到位"变成了一轮又一轮的重复失败。
#
# 所以每一条结论都要自带出处：谁在判、判的是哪份文件、那份文件是什么
# 哈希。而且**预检就红**的那种失败（一条用例都没跑）也要落盘 —— 今天
# 那条路径不写 artifact，于是 ``vp_artifacts/`` 里连 VP-015 都没有。


class TestVerifierProvenance:
    """出处随结论走 —— 正常路径与预检路径都带。"""

    def test_verifier_source_reports_the_module_on_disk(self):
        source = ci_runner.verifier_source()
        module_path = Path(ci_runner.__file__).resolve()
        assert source["module_path"] == str(module_path)
        assert source["sha256"] == hashlib.sha256(
            module_path.read_bytes()
        ).hexdigest()

    def test_a_passing_verdict_carries_the_provenance(self, tmp_path):
        result = run_ci_verification(_vp("exit 0"), project_dir=tmp_path)
        source = result.evidence["verifier_source"]
        assert source["module_path"] == str(
            Path(ci_runner.__file__).resolve()
        )
        assert source["sha256"]

    def test_a_schema_failure_carries_the_provenance(self, tmp_path):
        """预检就红的那条路径也要说得出"谁在判"。"""
        result = run_ci_verification(
            _vp("pytest tests/test_x.py::test_y"), project_dir=tmp_path,
        )
        assert result.status == STATUS_FAILED
        assert result.evidence["schema_issues"]
        assert result.evidence["verifier_source"]["sha256"]

    def test_a_schema_failure_leaves_an_artifact(self, tmp_path):
        """预检就红**也要落盘** —— 今天不落，于是 vp_artifacts 里没有它。"""
        artifact = tmp_path / "vp_artifacts"
        run_ci_verification(
            _vp("pytest tests/test_x.py::test_y"),
            project_dir=tmp_path,
            artifact_dir=artifact,
        )
        payload = json.loads(
            (artifact / "VP-901" / "ci_output.json").read_text(encoding="utf-8")
        )
        assert payload["schema_issues"]
        assert payload["verifier_source"]["sha256"]

    def test_the_artifact_names_the_module_it_came_from(self, tmp_path):
        artifact = tmp_path / "vp_artifacts"
        run_ci_verification(
            _vp("exit 0"), project_dir=tmp_path, artifact_dir=artifact,
        )
        payload = json.loads(
            (artifact / "VP-901" / "ci_output.json").read_text(encoding="utf-8")
        )
        assert payload["verifier_source"]["module_path"] == str(
            Path(ci_runner.__file__).resolve()
        )

    def test_the_verdict_says_which_copy_judged(self, tmp_path):
        """结论说得出"判的是哪一份"。

        被测树没有自带评分器（那不是框架仓的检出）→ 判的是 harness
        自己那份，结论就是这么说的。
        """
        result = run_ci_verification(_vp("exit 0"), project_dir=tmp_path)
        assert result.evidence["verifier_source"]["resolution"] == "harness"

    def test_the_artifact_says_which_copy_judged(self, tmp_path):
        """落盘的那份也得说得出 —— 磁盘上的证据是轮次之后唯一留下的。"""
        artifact = tmp_path / "vp_artifacts"
        run_ci_verification(
            _vp("exit 0"), project_dir=tmp_path, artifact_dir=artifact,
        )
        payload = json.loads(
            (artifact / "VP-901" / "ci_output.json").read_text(encoding="utf-8")
        )
        assert payload["verifier_source"]["resolution"] == "harness"


# ---------------------------------------------------------------------------
# venv 自举（2026-11-05，F3）
# ---------------------------------------------------------------------------
#
# ``scripts/run_tests.sh`` 硬要求 ``$PROJECT_ROOT/backend/.venv``，而
# "在一棵干净的工作树里跑整仓门禁"是常有的场景 —— 于是每个这样的场景
# 都要在入口里塞一段 ``ln -sfn <别的检出>/backend/.venv`` 的 shell hack，
# 而那段 hack 正是路径预检要猜"哪个操作数是生产出来的"的原因。
#
# 自举下沉到仓库之后，入口可以回归 ``CI=true bash scripts/run_tests.sh``：
# 没有分组括号、没有 ``ln``、没有指向仓外检出的绝对路径。


class TestSharedVenv:
    """门禁被告知可以复用哪个 venv。"""

    def test_a_shared_venv_is_offered_when_the_project_has_none(self, tmp_path):
        """被测树没有 venv → 给出 harness 自己那份。"""
        shared = ci_runner.shared_venv_for(tmp_path)
        if shared is None:
            pytest.skip("no venv available to share")
        assert Path(shared).is_dir()

    def test_the_project_venv_wins_over_the_shared_one(self, tmp_path):
        """被测树自己有 venv → 用它自己的。"""
        own = tmp_path / "backend" / ".venv"
        own.mkdir(parents=True)
        assert ci_runner.shared_venv_for(tmp_path) == str(own)

    def test_the_gate_receives_the_shared_venv(self, tmp_path):
        """门禁的环境里带着它 —— 入口脚本据此自举。"""
        shared = ci_runner.shared_venv_for(tmp_path)
        if shared is None:
            pytest.skip("no venv available to share")
        result = run_ci_verification(
            _vp("printenv PDT_SHARED_VENV"), project_dir=tmp_path,
        )
        assert result.status == STATUS_PASSED
        assert shared in result.evidence["stdout_head"]


_REPO_ROOT = _BACKEND_DIR.parent
_GATE_SCRIPT = _REPO_ROOT / "scripts" / "run_tests.sh"


def _fake_venv(root: Path) -> Path:
    """A venv that is only good enough for the gate to *start*."""
    venv = root / "shared-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "activate").write_text("# fake activation\n", encoding="utf-8")
    python = venv / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    return venv


def _fake_project(root: Path) -> Path:
    """A project root with the gate script in it and no venv."""
    project = root / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "backend").mkdir(parents=True)
    shutil.copy(_GATE_SCRIPT, project / "scripts" / "run_tests.sh")
    return project


class TestGateVenvBootstrap:
    """``scripts/run_tests.sh`` 的 venv 自举。"""

    def test_the_gate_links_the_shared_venv_when_the_project_has_none(
        self, tmp_path,
    ):
        """没有 venv、给了 PDT_SHARED_VENV → 软链进来，门禁起得来。"""
        project = _fake_project(tmp_path)
        shared = _fake_venv(tmp_path)
        env = dict(os.environ)
        env["PDT_SHARED_VENV"] = str(shared)
        env["CI"] = "true"

        subprocess.run(
            ["bash", "scripts/run_tests.sh", "--lane", "unit"],
            cwd=str(project), env=env, capture_output=True, text=True,
            timeout=180, check=False,
        )

        link = project / "backend" / ".venv"
        assert link.is_symlink()
        assert Path(os.readlink(link)) == shared

    def test_without_a_shared_venv_the_gate_says_how_to_fix_it(
        self, tmp_path,
    ):
        """没有 venv、也没有 PDT_SHARED_VENV → 报错文案说清两条出路。"""
        project = _fake_project(tmp_path)
        env = dict(os.environ)
        env.pop("PDT_SHARED_VENV", None)
        env["CI"] = "true"

        completed = subprocess.run(
            ["bash", "scripts/run_tests.sh", "--lane", "unit"],
            cwd=str(project), env=env, capture_output=True, text=True,
            timeout=180, check=False,
        )

        assert completed.returncode != 0
        assert "PDT_SHARED_VENV" in (completed.stdout + completed.stderr)
        assert not (project / "backend" / ".venv").exists()


# ---------------------------------------------------------------------------
# 失败的身份（2026-11-05 第 3 轮）
# ---------------------------------------------------------------------------
#
# 第 3 轮 VP-015 的失败指纹从 ``be0a4e421f361e5a`` 变成
# ``db929c18eeb90214`` —— 而**缺陷一个字都没变**：三轮的 schema detail
# 逐字节相同，三轮都被判成"换了根因"。
#
# 原因：被 schema 拒绝的 full_ci **一个用例都没跑过**，于是没有"机器写的
# 失败集合"，失败身份退回旧路 —— 旧路把报告证据的第一行**散文**算进
# 哈希。报告换个说法，指纹就变；于是"根因演进"这个判断被触发了两次，
# 两次都是假的。
#
# 这一组钉住：**失败的身份来自机器**（哪条规则、哪个对象），不来自散文。


class TestFailureIdentity:
    """预检就红的那条也得有机器写的失败集合。"""

    def test_a_schema_rejection_carries_a_machine_written_failing_set(
        self, tmp_path,
    ):
        """预检拒绝 → 结论里带着机器写的失败集合。"""
        result = run_ci_verification(
            _vp("bash scripts/does_not_exist.sh"), project_dir=tmp_path,
        )
        assert result.status == STATUS_FAILED
        assert result.evidence["failed_cases"] == [
            "schema:ci_entry_path_missing:scripts/does_not_exist.sh",
        ]

    def test_the_identity_is_the_rule_not_the_wording(self, tmp_path):
        """身份说得出"哪条规则、哪个对象"，不复述那句解释。"""
        result = run_ci_verification(
            _vp("bash scripts/does_not_exist.sh"), project_dir=tmp_path,
        )
        detail = result.evidence["schema_issues"][0]["detail"]
        identity = result.evidence["failed_cases"][0]
        assert detail not in identity
        assert identity.startswith("schema:")

    def test_the_same_defect_gives_the_same_identity(self, tmp_path):
        """同一个缺陷 → 同一个身份（措辞变不影响它）。"""
        first = run_ci_verification(
            _vp("bash scripts/does_not_exist.sh"), project_dir=tmp_path,
        )
        second = run_ci_verification(
            _vp("bash scripts/does_not_exist.sh"), project_dir=tmp_path,
        )
        assert first.evidence["failed_cases"] == second.evidence["failed_cases"]

    def test_a_different_defect_gives_a_different_identity(self, tmp_path):
        """不同的缺陷 → 不同的身份（不然指纹就分不出问题）。"""
        first = run_ci_verification(
            _vp("bash scripts/does_not_exist.sh"), project_dir=tmp_path,
        )
        second = run_ci_verification(
            _vp("bash scripts/other_missing.sh"), project_dir=tmp_path,
        )
        assert first.evidence["failed_cases"] != second.evidence["failed_cases"]

    def test_the_gate_path_carries_the_same_key(self, tmp_path):
        """门禁跑过的路径也带这个键 —— 键在两条路径上是一个意思。"""
        result = run_ci_verification(_vp("exit 0"), project_dir=tmp_path)
        assert result.evidence["failed_cases"] == []

    def test_the_artifact_carries_the_failing_set(self, tmp_path):
        """落盘的那份也得带 —— 磁盘上的证据是轮次之后唯一留下的。"""
        artifact = tmp_path / "vp_artifacts"
        run_ci_verification(
            _vp("bash scripts/does_not_exist.sh"),
            project_dir=tmp_path,
            artifact_dir=artifact,
        )
        payload = json.loads(
            (artifact / "VP-901" / "ci_output.json").read_text(encoding="utf-8")
        )
        assert payload["failed_cases"] == [
            "schema:ci_entry_path_missing:scripts/does_not_exist.sh",
        ]


# ---------------------------------------------------------------------------
# The plan → executor schema conversion (2026-11-06)
# ---------------------------------------------------------------------------


class TestPlanToExecutorSchemaKeepsE2eFields:
    """The e2e sub-class fields must survive the plan → executor schema.

    ``e2e_kind`` / ``entry_command`` are what the dispatch table reads
    (:func:`verification_subagent.resolve`): a **command** e2e is graded
    by the framework, a **browser** flow is not — it is escalated back to
    the LLM path.  ``cost`` is the cost gate's input.

    The conversion built the executor's VP from an **explicit field
    list**, and those three were not on it, so every e2e point reached
    the dispatch table as a browser flow (2026-11-05: the deterministic
    plan-flow's e2e went to a scripted LLM session and failed there).

    The field names are pinned by the module under test
    (``verification_ci_runner``), which is why this spec lives here.
    """

    PLAN = {
        "verification_points": [
            {
                "id": "VP-004",
                "verification_method": "e2e",
                "title": "从列表页走到结算页",
                "expected_result": "结算完成页出现确认文案",
                "e2e_kind": "command",
                "entry_command": "python3 scripts/e2e_local.py",
                "cost": "none",
            },
        ],
    }

    @staticmethod
    def _converted():
        from verification_agent import Orchestrator

        return Orchestrator._convert_plan_to_executor_schema(
            TestPlanToExecutorSchemaKeepsE2eFields.PLAN,
        )["vps"]

    def test_the_conversion_carries_the_three_fields(self):
        vps = self._converted()
        assert vps[0]["e2e_kind"] == "command"
        assert vps[0]["entry_command"] == "python3 scripts/e2e_local.py"
        assert vps[0]["cost"] == "none"

    def test_the_runner_reads_the_converted_point_as_a_command_e2e(self):
        """The runner must not read the converted point as a browser flow."""
        converted = dict(self._converted()[0])
        # The dispatch closure translates the executor schema's ``method``
        # back into the LLM schema's ``verification_method``
        # (``verification_agent``) — the runner dispatches on the latter.
        converted["verification_method"] = converted["method"]
        assert ci_runner.e2e_kind(converted) == ci_runner.E2E_KIND_COMMAND
        assert validate_vp(converted) == []


# ---------------------------------------------------------------------------
# validate_vp — self-healing entries (2026-11-01) under the plan-time
# shape contract (2026-11-05, arch 决策点 2)
# ---------------------------------------------------------------------------
#
# VP-018's entry creates the venv it then reads:
#
#     test -d backend/.venv || ln -s <elsewhere>/backend/.venv backend/.venv;
#     bash scripts/run_tests.sh --lane all
#
# The path reader treated *every* path-like token as an input, so the
# venv the entry brings into being itself was reported as "does not
# exist" — the gate was rejected at the schema stage and not one
# command ran.  The path was never missing: the entry creates it.
#
# The distinction that fixes it: a path the entry **creates** is not an
# input it depends on; a path it only **reads** still is.  Both halves
# are pinned below — the exemption must not become a blanket amnesty.
#
# The entry is an inline compound command, so the assertions about the
# path semantics run it in its **执行期** form
# (``require_single_entry=False``): the 形态契约 (2026-11-05) is a
# *planning* contract — a plan that is written today must name one call
# to the project adapter script — while the historical plans are not
# rewritten and their compound entries still have to run.  The planning
# side of the same entry is pinned in
# :meth:`TestValidateVpSelfHealingEntry.test_the_same_entry_is_rejected_at_plan_time`.


def _declared_vp018():
    """VP-018 as the plan declares it — ``None`` when it is unreachable.

    The plan is not part of this repository; the executor points at it
    through ``PDT_PLAN_DIR``.
    """
    plan_dir = os.environ.get("PDT_PLAN_DIR")
    if not plan_dir:
        return None
    plan_file = Path(plan_dir) / "verification_plan.json"
    if not plan_file.is_file():
        return None
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    for vp in plan.get("verification_points", []):
        if vp.get("id") == "VP-018":
            return vp
    return None


def _self_healing_entry() -> str:
    """VP-018's entry, rebuilt.

    The symlink's source is an absolute path — outside the repo, and
    never existence-checked — so the entry is built at run time instead
    of being written into this file as one developer's home directory.
    """
    source = Path.home() / "backend" / ".venv"
    return (f"test -d backend/.venv || ln -s {source} backend/.venv; "
            "bash scripts/run_tests.sh --lane all")


class TestValidateVpSelfHealingEntry:
    def test_entry_that_creates_its_own_venv_is_not_a_schema_issue(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run_tests.sh").write_text("#!/bin/sh\n")
        entry = (
            "test -d backend/.venv || ln -s /somewhere/else/backend/.venv "
            "backend/.venv; bash scripts/run_tests.sh --lane all"
        )
        # 执行期形态：复合入口照旧要能跑，所以这里关掉「必须是对适配脚本的
        # 一次调用」那道**规划期**契约（``require_single_entry=False``，
        # 见 ``validate_vp`` 的 docstring）—— 下面断言的是**路径**语义。
        assert validate_vp(
            _vp(entry), project_dir=tmp_path, require_single_entry=False,
        ) == []

    def test_the_same_entry_is_rejected_at_plan_time(self, tmp_path):
        # 规划期形态契约（2026-11-05, arch 决策点 2）：同一条复合入口在
        # 规划期被退回 —— 新写的计划必须是对适配脚本的一次调用。
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run_tests.sh").write_text("#!/bin/sh\n")
        entry = (
            "test -d backend/.venv || ln -s /somewhere/else/backend/.venv "
            "backend/.venv; bash scripts/run_tests.sh --lane all"
        )
        issues = validate_vp(_vp(entry), project_dir=tmp_path)
        assert len(issues) == 1
        assert "含 shell 操作符" in issues[0].detail
        assert "||" in issues[0].detail

    def test_a_single_command_that_creates_a_path_passes_the_plan_time_check(
        self, tmp_path,
    ):
        # 两条契约合起来的样子：规划期只认「一次调用」，执行期认「这条
        # 入口自己建出来的路径不算输入」。一次调用 + 自建路径，两条都满足。
        assert validate_vp(
            _vp("ln -sfn /tmp/somewhere-venv backend/.venv"),
            project_dir=tmp_path,
        ) == []

    def test_probe_without_creation_is_still_an_issue(self, tmp_path):
        # Negative control: without the creating command the venv is an
        # input, and a missing input is still an issue.
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run_tests.sh").write_text("#!/bin/sh\n")
        entry = "test -d backend/.venv && bash scripts/run_tests.sh --lane all"
        issues = validate_vp(
            _vp(entry), project_dir=tmp_path, require_single_entry=False,
        )
        assert len(issues) == 1
        assert "backend/.venv" in issues[0].detail
        assert "不存在" in issues[0].detail

    def test_selfhealing_entry_still_requires_the_gate_script(self, tmp_path):
        # The exemption must not carry over to the script the gate runs:
        # a self-healing entry still needs the entry script to exist.
        entry = (
            "test -d backend/.venv || ln -s /somewhere/else/backend/.venv "
            "backend/.venv; bash scripts/run_tests.sh --lane all"
        )
        issues = validate_vp(
            _vp(entry), project_dir=tmp_path, require_single_entry=False,
        )
        assert len(issues) == 1
        assert "scripts/run_tests.sh" in issues[0].detail
        assert "不存在" in issues[0].detail

    def test_existing_created_path_is_still_fine(self, tmp_path):
        # The venv is already there — nothing is created, nothing to
        # complain about.
        (tmp_path / "backend" / ".venv").mkdir(parents=True)
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run_tests.sh").write_text("#!/bin/sh\n")
        entry = (
            "test -d backend/.venv || ln -s /somewhere/else/backend/.venv "
            "backend/.venv; bash scripts/run_tests.sh --lane all"
        )
        assert validate_vp(
            _vp(entry), project_dir=tmp_path, require_single_entry=False,
        ) == []

    def test_the_source_of_a_created_path_is_still_required(self, tmp_path):
        # ``ln -s <src> <dest>`` **reads** its source — only the
        # destination is something the entry creates.  The exemption must
        # not reach the source.
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run_tests.sh").write_text("#!/bin/sh\n")
        entry = (
            "ln -s missing/backend/.venv backend/.venv; "
            "bash scripts/run_tests.sh --lane all"
        )
        issues = validate_vp(
            _vp(entry), project_dir=tmp_path, require_single_entry=False,
        )
        assert [i.detail for i in issues] == [
            "ci_entry 引用的路径不存在：missing/backend/.venv",
        ]


class TestValidateVpAgainstTheRealCriteria:
    """The criteria as they are actually declared, replayed.

    The narrow cases above use a ``tmp_path`` and a hand-written entry.
    This class replays the *real* one: VP-018's ``ci_entry``, read out of
    the plan's own ``verification_plan.json``, against the *real* project
    root — the directory that holds ``scripts/run_tests.sh``.

    The plan is not part of this repository, so the executor points at it
    through ``PDT_PLAN_DIR``.  Where it is not reachable the entry's shape
    is rebuilt instead, which pins the same behaviour everywhere.
    """

    def test_the_declared_vp018_entry_is_not_a_schema_issue(self):
        vp = _declared_vp018()
        if vp is None:
            pytest.skip(
                "the plan's verification_plan.json is not reachable from here"
            )
        assert validate_vp(
            vp, project_dir=_REPO_ROOT, require_single_entry=False,
        ) == []

    def test_a_self_healing_entry_in_the_repo_root_is_not_a_schema_issue(self):
        # The same entry, rebuilt: the symlink's source is an absolute
        # path — outside the repo, and never existence-checked — so it is
        # built at run time instead of being written into this file as one
        # developer's home directory.
        assert validate_vp(
            _vp(_self_healing_entry()), project_dir=_REPO_ROOT,
            require_single_entry=False,
        ) == []

    def test_the_gate_script_of_the_real_entry_is_still_required(self):
        # The exemption covers the venv the entry creates and nothing else:
        # the script the gate runs is still an input.
        entry = _self_healing_entry().replace(
            "scripts/run_tests.sh", "scripts/does_not_exist.sh",
        )
        issues = validate_vp(
            _vp(entry), project_dir=_REPO_ROOT, require_single_entry=False,
        )
        assert [i.detail for i in issues] == [
            "ci_entry 引用的路径不存在：scripts/does_not_exist.sh",
        ]


class TestCreatedPaths:
    """The paths an entry brings into being itself."""

    def test_ln_destination_is_created(self):
        created = _created_paths("ln -s /somewhere/.venv backend/.venv")
        assert "backend/.venv" in created

    def test_mkdir_target_is_created(self):
        assert "backend/.venv" in _created_paths("mkdir -p backend/.venv")

    def test_touch_target_is_created(self):
        assert "backend/marker" in _created_paths("touch backend/marker")

    def test_cp_destination_is_created_but_its_source_is_not(self):
        created = _created_paths("cp scripts/a.sh scripts/b.sh")
        assert "scripts/b.sh" in created
        assert "scripts/a.sh" not in created

    def test_mv_destination_is_created(self):
        created = _created_paths("mv scripts/a.sh scripts/b.sh")
        assert "scripts/b.sh" in created
        assert "scripts/a.sh" not in created

    def test_install_destination_is_created(self):
        created = _created_paths("install -m 755 scripts/a.sh scripts/b.sh")
        assert "scripts/b.sh" in created

    def test_created_path_after_cd_is_resolved_against_it(self):
        created = _created_paths("cd backend && ln -s /somewhere/.venv .venv")
        assert "backend/.venv" in created

    def test_created_path_inside_a_shell_payload_is_created(self):
        created = _created_paths(
            "bash -c 'cd backend && ln -s /somewhere/.venv .venv'"
        )
        assert "backend/.venv" in created

    def test_a_command_that_creates_nothing_creates_no_path(self):
        assert _created_paths("bash scripts/run_tests.sh --lane all") == set()

    def test_the_glued_separator_ends_the_command_that_carries_it(self):
        # ``backend/.venv;`` —— 路径和分隔符是**同一个** token，所以这条
        # 命令到 ``;`` 就结束了：``ln`` 建的是 ``backend/.venv``，不是
        # 下一条命令的 ``all``。
        created = _created_paths(
            "ln -s /somewhere/.venv backend/.venv; bash scripts/run_tests.sh --lane all"
        )
        assert created == {"backend/.venv"}

# 轮次归属 — 取证副本按轮次另存，轮次写进载荷
# ---------------------------------------------------------------------------
#
# 一个 VP 的产物目录是**跨轮复用**的（2026-11-08）：后一轮的
# ``ci_output.json`` 覆盖了前一轮的，于是同一束证据自相矛盾 —— 产物文件里
# 记的是后一轮的退出码，同目录的 gate 日志里记的是前一轮的结论。没有轮次
# 归属的取证，谁也说不清自己引用的是哪一轮。
#
# 所以：``ci_output.json`` 的路径与语义一个字不改（它仍然是 full_ci 的
# 唯一判据），但同一次运行**另存**一份 ``ci_output.round-<n>.json``，
# 轮次号写进载荷，后一轮不覆盖前一轮的副本。


class TestRoundScopedArtifact:
    def test_the_round_copy_is_written_next_to_the_live_verdict(self, tmp_path):
        artifact = tmp_path / "vp_artifacts"

        result = run_ci_verification(
            _vp("exit 0"), project_dir=tmp_path, artifact_dir=artifact,
            round_no=7,
        )

        assert result.status == STATUS_PASSED
        copy = artifact / "VP-901" / "ci_output.round-7.json"
        assert copy.is_file()
        record = json.loads(copy.read_text(encoding="utf-8"))
        assert record["exit_code"] == 0
        assert record["round"] == 7

    def test_the_live_verdict_says_which_round_it_belongs_to(self, tmp_path):
        """The live verdict names no round; the copy beside it does.

        The merged contract (2026-11-09): the runner writes the payload
        it was given, byte for byte, so ``ci_output.json`` carries the
        contract fields and **no key of its own** — a round label in
        there would make the artifact a copy of itself.  The round
        bookkeeping lives in the per-round copy, and that is the file
        that says which round the evidence belongs to.
        """
        artifact = tmp_path / "vp_artifacts"

        result = run_ci_verification(
            _vp("exit 0"), project_dir=tmp_path, artifact_dir=artifact,
            round_no=7,
        )

        assert "round" not in result.evidence
        assert "round_source" not in result.evidence
        payload = json.loads(
            (artifact / "VP-901" / "ci_output.json").read_text(encoding="utf-8")
        )
        assert "round" not in payload
        assert "round_source" not in payload

        # The copy beside it is the file that names the round ...
        copy = json.loads(
            (artifact / "VP-901" / "ci_output.round-7.json").read_text(
                encoding="utf-8"
            )
        )
        assert copy["round"] == 7
        assert copy["round_source"] == "parameter"
        # ... and it differs from the live verdict by that bookkeeping
        # and by nothing else: a reader of the copy is reading the same
        # run the artifact describes.
        stripped = {
            key: value for key, value in copy.items()
            if key not in ("round", "round_source")
        }
        assert stripped == payload

    def test_a_later_round_does_not_overwrite_an_earlier_copy(self, tmp_path):
        """The earlier round's copy survives — and the round is named by
        the copies, not by the live verdict.

        The live verdict is the newest round's — that is what it is for
        — and under the merged contract it names no round at all: the
        round each piece of evidence belongs to is written on the copy
        beside it.
        """
        artifact = tmp_path / "vp_artifacts"

        run_ci_verification(
            _vp("exit 0"), project_dir=tmp_path, artifact_dir=artifact,
            round_no=1,
        )
        run_ci_verification(
            _vp("exit 1"), project_dir=tmp_path, artifact_dir=artifact,
            round_no=2,
        )

        earlier = json.loads(
            (artifact / "VP-901" / "ci_output.round-1.json").read_text(
                encoding="utf-8"
            )
        )
        assert earlier["exit_code"] == 0
        assert earlier["round"] == 1
        later = json.loads(
            (artifact / "VP-901" / "ci_output.round-2.json").read_text(
                encoding="utf-8"
            )
        )
        assert later["exit_code"] == 1
        assert later["round"] == 2
        # the live verdict is the newest round's — that is what it is for
        live = json.loads(
            (artifact / "VP-901" / "ci_output.json").read_text(encoding="utf-8")
        )
        assert live["exit_code"] == 1
        assert "round" not in live
        assert "round_source" not in live

    def test_the_schema_failure_path_says_the_round_too(self, tmp_path):
        """A round that never ran a case left an artifact too — and that
        artifact belongs to a round as well.

        The round is named by the per-round copy: ``ci_output.json``
        carries the contract fields only — the writer adds no key of
        its own (2026-11-09).
        """
        artifact = tmp_path / "vp_artifacts"
        vp = {"id": "VP-901", "verification_method": "full_ci",
              "verification_phase": 2}

        result = run_ci_verification(
            vp, project_dir=tmp_path, artifact_dir=artifact, round_no=4,
        )

        assert "round" not in result.evidence
        payload = json.loads(
            (artifact / "VP-901" / "ci_output.json").read_text(encoding="utf-8")
        )
        assert "round" not in payload
        copy = json.loads(
            (artifact / "VP-901" / "ci_output.round-4.json").read_text(
                encoding="utf-8"
            )
        )
        assert copy["round"] == 4
        assert copy["round_source"] == "parameter"

    def test_a_copy_that_cannot_be_written_does_not_change_the_verdict(
        self, tmp_path, monkeypatch,
    ):
        """取证副本写不出来不得改变判决 —— 判决在写副本之前就定了。"""
        artifact = tmp_path / "vp_artifacts"

        def _boom(*args, **kwargs):
            raise OSError("no space left on device")

        monkeypatch.setattr(ci_runner, "_write_round_copy", _boom)

        result = run_ci_verification(
            _vp("exit 0"), project_dir=tmp_path, artifact_dir=artifact,
            round_no=7,
        )

        assert result.status == STATUS_PASSED
        assert (artifact / "VP-901" / "ci_output.json").is_file()

    def test_no_temporary_file_is_left_behind(self, tmp_path):
        artifact = tmp_path / "vp_artifacts"

        run_ci_verification(
            _vp("exit 0"), project_dir=tmp_path, artifact_dir=artifact,
            round_no=7,
        )

        assert not list((artifact / "VP-901").glob("*.tmp"))


# ---------------------------------------------------------------------------
# 轮次从哪来 —— agent 知道自己正在跑第几轮
# ---------------------------------------------------------------------------


def test_the_agent_tells_the_runner_which_round_it_is(tmp_path, monkeypatch):
    """产物属于哪一轮，得由跑它的那一轮说出来 —— agent 知道自己是第几轮。"""
    captured = {}

    def _fake_run(vp, *, project_dir, artifact_dir=None, round_no=None):
        captured["round_no"] = round_no
        return ci_runner.CiRunResult(
            status=STATUS_PASSED,
            reasons=["[full_ci] `exit 0` 退出码 0（用时 0s）"],
            evidence={"vp_id": "VP-901", "exit_code": 0},
        )

    monkeypatch.setattr(ci_runner, "run_ci_verification", _fake_run)
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))

    agent = _full_ci_agent(tmp_path)
    agent._current_round = 5

    agent._run_full_ci_vp(_full_ci_vp())

    assert captured["round_no"] == 5


def test_a_runner_that_cannot_be_told_the_round_still_works(tmp_path, monkeypatch):
    """项目自带的 runner 副本可能还没有这个参数 —— 不能因此崩掉一轮。"""
    def _fake_run(vp, *, project_dir, artifact_dir=None):
        return ci_runner.CiRunResult(
            status=STATUS_PASSED,
            reasons=["[full_ci] `exit 0` 退出码 0（用时 0s）"],
            evidence={"vp_id": "VP-901", "exit_code": 0},
        )

    monkeypatch.setattr(ci_runner, "run_ci_verification", _fake_run)
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))

    agent = _full_ci_agent(tmp_path)
    agent._current_round = 5

    verdict = agent._run_full_ci_vp(_full_ci_vp())

    assert verdict["status"] == STATUS_PASSED, verdict
