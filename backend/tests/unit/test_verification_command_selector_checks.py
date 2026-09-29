"""V2/V3 —— 命令点名的测试目标必须真实存在（2026-09-15）。

任务侧早就有这道检查（``tasks_generator.TaskOutputValidator`` 的 Step 4
``test_command_function_name_check``：AST 解析目标文件、确认函数定义在里面）。
VP 侧一直缺，于是出现了三例"命令看起来跑了、其实什么都没验"：VP-013 的
``cargo test --lib <集成测试名>``、VP-017 扫一个不存在的目录、VP-020 采样
0 个 fixture。

设计上最要紧的一条是**匹配语义必须和 runner 的真实行为对齐**：

* cargo 的过滤名、pytest 的 ``-k`` 都是**子串**匹配
  （``cargo test --test f test_a`` 会跑 ``fn test_a_signal_fields()``）；
* 只有 pytest 的 ``<文件>::<用例>`` 是精确节点 ID。

初版按精确名找，把真实计划里的 VP-025 / VP-026 判成了违规 —— 实测
确认是误报。**这类检查一旦开始喊狼来了，LLM 就会为了过检查而空转，比不做
更糟**（同一教训在"每条命令都要有显式时间上限"上已经吃过一次）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verification_command_guard import (
    find_violations,
    needs_semantic_review,
)


def _codes(cmd, project_dir=None):
    return [v.code for v in find_violations(cmd, project_dir)]


# ---------------------------------------------------------------------------
# 造一个 tmp 项目：src/ 与 tests/ 各有一个测试
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """``native_ext/`` 下：``src/lib.rs`` 定义 test_in_src，
    ``tests/signal_classification.rs`` 定义 test_a_signal_fields。"""
    crate = tmp_path / "native_ext"
    (crate / "src").mkdir(parents=True)
    (crate / "tests").mkdir(parents=True)
    (crate / "src" / "lib.rs").write_text(
        "#[cfg(test)]\nmod tests {\n    #[test]\n    fn test_in_src() {}\n}\n",
        encoding="utf-8",
    )
    (crate / "tests" / "signal_classification.rs").write_text(
        "#[test]\nfn test_a_signal_fields() {}\n",
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# V2：点名的目标必须存在
# ---------------------------------------------------------------------------


def test_pytest_naming_a_missing_file_is_flagged(project: Path) -> None:
    assert "missing_test_target" in _codes(
        "source venv1/bin/activate && pytest tests/nope.py -v", project,
    )


def test_pytest_naming_a_real_file_is_fine(project: Path) -> None:
    (project / "tests").mkdir()
    (project / "tests" / "test_ok.py").write_text("def test_x(): pass\n", encoding="utf-8")
    assert _codes("pytest tests/test_ok.py -v", project) == []


def test_cargo_naming_a_missing_integration_file_is_flagged(project: Path) -> None:
    assert "missing_test_target" in _codes(
        "cd native_ext && cargo test --test no_such_file test_x",
        project,
    )


def test_without_project_dir_there_are_no_existence_checks() -> None:
    """不给项目目录 → 退化成纯语法检查（保持模块可独立使用）。"""
    assert _codes("pytest tests/does_not_exist_anywhere.py -v") == []


# ---------------------------------------------------------------------------
# V3 的核心：作用域 + 子串语义
# ---------------------------------------------------------------------------


def test_cargo_lib_scope_does_not_see_tests_dir(project: Path) -> None:
    """``--lib`` 只搜 ``src/``；点名一个住在 ``tests/`` 的测试 = 匹配不到。

    这正是 VP-013 的形状：命令编译成功、匹配 0 个测试、退出 0，什么都没验。
    """
    codes = _codes(
        'bash -c "source venv1/bin/activate && cd native_ext && '
        'cargo test --lib test_a_signal_fields -- --nocapture"',
        project,
    )
    assert "unknown_test_identifier" in codes


def test_cargo_test_scope_sees_the_named_integration_file(project: Path) -> None:
    assert _codes(
        "cd native_ext && cargo test --test signal_classification test_a_signal_fields",
        project,
    ) == []


def test_cargo_lib_scope_sees_its_own_test(project: Path) -> None:
    assert _codes("cd native_ext && cargo test --lib test_in_src", project) == []


def test_cargo_filter_is_a_substring_match(project: Path) -> None:
    """回归锁（VP-025 / VP-026 的真实误报）。

    真实测试名是 ``test_a_signal_fields``，命令写 ``test_a`` —— cargo 会正常
    命中。按精确名找会把这类命令全判成违规。
    """
    assert _codes(
        "cd native_ext && cargo test --test signal_classification test_a",
        project,
    ) == [], "cargo 的过滤名是子串语义，短名字是合法的"


def test_cargo_filter_matching_nothing_at_all_is_flagged(project: Path) -> None:
    assert "unknown_test_identifier" in _codes(
        "cd native_ext && cargo test --test signal_classification test_zzz_absent",
        project,
    )


def test_pytest_k_expression_is_a_substring_match(project: Path) -> None:
    """``-k`` 和 cargo 的过滤名同语义：短名字只要是真名的子串就合法。"""
    assert "unknown_test_identifier" not in _codes(
        'cd native_ext && pytest tests/ -k "test_a"', project,
    )


def test_pytest_k_expression_naming_nothing_is_flagged(project: Path) -> None:
    assert "unknown_test_identifier" in _codes(
        'cd native_ext && pytest tests/ -k "test_zzz_absent"', project,
    )


# ---------------------------------------------------------------------------
# 需要 LLM 语义兜底的情形（没有标准 runner）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    'bash -c "source venv1/bin/activate && python tools/verify_fixture_integrity.py"',
    "bash tests/e2e/test_pipeline_e2e.sh 2>&1; echo EXIT_CODE=$?",
    "bash -c \"grep -rn 'EXAMPLE' frontend/ || echo CLEAN\"",
    "curl -s 'http://127.0.0.1:8080/api/items/1min/EXAMPLE' | jq .",
])
def test_custom_scripts_need_semantic_review(cmd: str) -> None:
    """用户 2026-09-15：静态判不了边界的，交给 LLM 理解语义。"""
    assert needs_semantic_review(cmd) is True


def test_statically_flagged_command_skips_semantic_review() -> None:
    """已经静态判出结论的命令，不再花钱做语义审查。

    2026-09-17：``git diff … | grep -A 50 'foo'`` 以前静态层认不出来，
    所以进了语义兜底。现在管道规则认得它了——退出码是 ``grep`` 的，无论
    diff 内容如何都无法报失败。按本模块的约定（"结论已经拿到了，没必要
    再花钱"），这种命令应当去**重写**，而不是再要一份模型意见。
    """
    cmd = "git diff HEAD~1..HEAD -- src/x.rs | grep -A 50 'foo'"
    assert "upstream_failure_masked_by_filter" in [
        v.code for v in find_violations(cmd)
    ]
    assert needs_semantic_review(cmd) is False


@pytest.mark.parametrize("cmd", [
    "cd native_ext && cargo test --test signal_classification test_a",
    "pytest tests/api/test_foo.py -v",
    "pytest tests/ -k 'test_a'",
])
def test_standard_runners_do_not_need_semantic_review(cmd: str) -> None:
    assert needs_semantic_review(cmd) is False


def test_an_already_flagged_command_skips_semantic_review() -> None:
    """已经静态判出违规的，不必再花钱问 LLM —— 结论已经拿到了。"""
    assert needs_semantic_review("docker compose up -d && pytest tests/ -v") is False
    assert needs_semantic_review("cargo test --workspace") is False


@pytest.mark.parametrize("cmd", [None, "", "   "])
def test_empty_commands_never_need_review(cmd) -> None:
    assert needs_semantic_review(cmd) is False


@pytest.mark.parametrize("cmd", ["echo ok", "echo 'All passed'", "true", "exit 0"])
def test_degenerate_commands_are_not_sent_to_the_llm(cmd: str) -> None:
    """``echo ok`` 什么都没验 —— 但那是静态层**一眼能判**的，不是"判不了"。

    前提是静态层判不了 —— 判得了的就不该送进 LLM。把它也
    送进去有两个坏处：白花一次 high 档调用；以及大量以 ``echo`` 占位的测试
    夹具凭空多打一轮（实测本仓库的单测里有 24 处这种夹具）。
    """
    assert needs_semantic_review(cmd) is False
