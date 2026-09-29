"""
Tests for the 2026-09-15 task-generation hardening fixes.

Four defects were found while auditing the tasks generated for plan
``2026-09-15 plan`` and traced back to the generator /
validator pipeline. Each test here pins one fix so the behaviour
cannot silently regress:

1. **Coverage detector kind collision** — the programmatic
   ``missing_coverage`` check searched for a bare ``决策点 N`` pattern,
   so an ``arch 决策点 3`` citation also satisfied ``test 决策点 3``
   (the two documents number independently) and real gaps on the test
   side could be masked by unrelated arch references.

2. **Validator accepts empty test_command** — ``SubTask.test_command``
   defaults to ``""``, so the schema check's ``is None`` loop could
   never fire; step 4 then no-ops on empty input, silently disabling
   the command-line half of the dual-signal completion rule.

3. **Extensionless module paths never extracted** — the path regex
   requires a file extension, but the generator writes Python module
   paths without one (``backend/failure_miner/normalizer``), so
   ``files_to_modify`` degraded to the sentinel for nearly every task
   (which in turn spuriously tagged them ``verification_only``).

4. **Missing test_command never backfilled** — ``files_to_modify`` had
   a description-scanning backfill but ``test_command`` had none, even
   though the TDD 规格 sections name the test files.

5. **Semantic package dependencies lost** — dependency inference is
   prose-driven; "modules inside a package depend on the task that
   creates the package" never appears in prose, so the edge was absent
   from the graph (tasks declared 前置条件：无 while creating files
   inside another task's new package).

6. **Generation prompt contract** — the prompt must ask for the
   decision-point citations (1) and the package-ownership dependency
   rule (5); without them the deterministic checks above check a
   convention the generator was never told to honour.
"""

from __future__ import annotations

from pathlib import Path

import pytest


class _StubCodingTool:
    """Minimal stand-in for the coding tool — not exercised here."""

    def __init__(self, response: dict):
        self._response = response

    def run_query(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


def _make_gen(plan_dir: Path):
    from tasks_generator import TasksGenerator

    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "prd.md").write_text("# PRD\n", encoding="utf-8")
    return TasksGenerator(
        coding_tool=_StubCodingTool(response={}), plan_dir=plan_dir
    )


@pytest.fixture
def gen(tmp_path):
    """Generator with no repo context around it (``_find_repo_root``
    returns None → the pass-2b parent guard is skipped)."""
    return _make_gen(tmp_path / "plans" / "plan-x")


@pytest.fixture
def repo_gen(tmp_path):
    """Generator whose plan_dir lives inside a synthetic repo:
    ``backend/tests/unit/`` exists, ``backend/failure_miner/`` does
    not — the exact shape of the Failure-Miner plan at generation
    time."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "backend" / "tests" / "unit").mkdir(parents=True)
    return _make_gen(tmp_path / "plans" / "plan-x")


# ---------------------------------------------------------------------------
# 1. Coverage detector must distinguish arch / test citations
# ---------------------------------------------------------------------------


def test_coverage_detector_distinguishes_kind(tmp_path):
    plan_dir = tmp_path / "plans" / "plan-x"
    plan_dir.mkdir(parents=True)
    (plan_dir / "prd.md").write_text("# PRD\n", encoding="utf-8")
    (plan_dir / "arch-design.md").write_text(
        "### 决策点 1: 组件边界\n### 决策点 3: taxonomy 配置化\n",
        encoding="utf-8",
    )
    (plan_dir / "test-design.md").write_text(
        "### 决策点 3: 归一化等价类\n", encoding="utf-8"
    )
    tool = _StubCodingTool(response={})
    from tasks_generator import TasksGenerator

    gen = TasksGenerator(coding_tool=tool, plan_dir=plan_dir)
    tasks = [
        {
            "id": "1",
            # cites arch 1 and arch 3 by their kind-prefixed form
            "description": "实施 arch 决策点 1 与 arch 决策点 3 的内容",
            "files_to_modify": ["backend/agent.py"],
        }
    ]
    findings = gen._compute_decision_coverage_findings({"tasks": tasks})
    locations = {f["location"] for f in findings}
    # arch side fully covered…
    assert "arch 决策点 1" not in locations
    assert "arch 决策点 3" not in locations
    # …but the bare "决策点 3" number must NOT satisfy test 决策点 3.
    assert "test 决策点 3" in locations


def test_coverage_detector_accepts_kind_prefixed_citations(tmp_path):
    plan_dir = tmp_path / "plans" / "plan-x"
    plan_dir.mkdir(parents=True)
    (plan_dir / "prd.md").write_text("# PRD\n", encoding="utf-8")
    (plan_dir / "arch-design.md").write_text(
        "### 决策点 1: 组件边界\n", encoding="utf-8"
    )
    (plan_dir / "test-design.md").write_text(
        "### 决策点 1: 单测策略\n", encoding="utf-8"
    )
    from tasks_generator import TasksGenerator

    gen = TasksGenerator(
        coding_tool=_StubCodingTool(response={}), plan_dir=plan_dir
    )
    tasks = [
        {
            "id": "1",
            "description": "## 对应上游决策点\n- arch 决策点 1：组件边界\n- test 决策点 1：单测策略",
            "files_to_modify": ["backend/agent.py"],
        }
    ]
    findings = gen._compute_decision_coverage_findings({"tasks": tasks})
    assert findings == []


# ---------------------------------------------------------------------------
# 2. Validator must reject an empty test command
# ---------------------------------------------------------------------------


def test_validator_rejects_empty_test_command(tmp_path):
    from framework.task_output_validator import (
        TaskOutputValidator,
        TaskValidationError,
    )
    from task import SubTask

    v = TaskOutputValidator(tmp_path)
    task = SubTask(
        id="1",
        title="t",
        description="d",
        # model default "" — the pre-fix schema check let this through
        files_to_modify=["backend/agent.py"],
        depends_on=[],
    )
    with pytest.raises(TaskValidationError) as exc_info:
        v._json_schema_check(task)
    assert "test_command" in str(exc_info.value)


def test_validator_accepts_test_commands_list_form(tmp_path):
    from framework.task_output_validator import TaskOutputValidator
    from task import SubTask

    v = TaskOutputValidator(tmp_path)
    task = SubTask(
        id="1",
        title="t",
        description="d",
        test_commands=["pytest tests/unit/test_a.py -v"],
        files_to_modify=["backend/agent.py"],
        depends_on=[],
    )
    v._json_schema_check(task)  # must not raise


# ---------------------------------------------------------------------------
# 3. Extensionless module paths → files_to_modify (parent-guarded)
# ---------------------------------------------------------------------------


def test_backfill_extracts_extensionless_module_paths(repo_gen):
    tasks = [
        {
            "id": "3",
            "description": (
                "## 修改位置\n"
                "- 新建模块 `backend/failure_miner/normalizer`\n"
                "- 新建测试 `backend/tests/unit/test_normalizer`"
            ),
            "files_to_modify": [],
        }
    ]
    out = repo_gen._backfill_files_to_modify_from_description(tasks)
    files = out[0]["files_to_modify"]
    # test file: parent (backend/tests/unit/) exists in the repo → extracted
    assert "backend/tests/unit/test_normalizer.py" in files
    # module: parent (backend/failure_miner/) does NOT exist yet → the
    # speculative .py path must NOT be emitted (it would fail validator
    # step-3 at dispatch); the sentinel is kept for the fill loop.
    assert "backend/failure_miner/normalizer.py" not in files
    assert "__UNKNOWN_MODIFICATIONS__" in files


def test_backfill_module_path_skips_directory_mentions(repo_gen):
    tasks = [
        {
            "id": "1",
            "description": "新建包目录 `backend/failure_miner/`，含包初始化模块",
            "files_to_modify": [],
        }
    ]
    out = repo_gen._backfill_files_to_modify_from_description(tasks)
    files = out[0]["files_to_modify"]
    # A trailing slash means "the directory", not a file inside it —
    # `backend/failure_miner.py` would be a wrong guess.
    assert "backend/failure_miner.py" not in files


def test_backfill_module_path_skips_directory_without_slash(repo_gen):
    """``新建包目录 X`` without a trailing slash is still a directory."""
    tasks = [
        {
            "id": "1",
            "description": "新建包目录 `backend/failure_miner`，含包初始化模块",
            "files_to_modify": [],
        }
    ]
    out = repo_gen._backfill_files_to_modify_from_description(tasks)
    assert "backend/failure_miner.py" not in out[0]["files_to_modify"]


def test_backfill_module_path_does_not_clobber_real_extensions(repo_gen):
    """``scripts/runners/run_x.sh`` is a real file — the module-path pass
    must not invent ``…/run_x.py`` for it (validator step-3 would accept
    the speculative path via the "parent exists" rule and the executor
    would build the wrong artefact)."""
    (repo_gen.plan_dir.parent.parent / "scripts" / "runners").mkdir(
        parents=True
    )
    tasks = [
        {
            "id": "18",
            "description": (
                "## 修改位置\n"
                "- 新建运行脚本 `scripts/runners/run_failure_miner.sh`"
            ),
            "files_to_modify": [],
        }
    ]
    out = repo_gen._backfill_files_to_modify_from_description(tasks)
    files = out[0]["files_to_modify"]
    assert "scripts/runners/run_failure_miner.sh" in files
    assert "scripts/runners/run_failure_miner.py" not in files


def test_backfill_test_command_requires_pytest_naming(gen):
    """Only ``test_*.py`` / ``conftest.py`` count — fixture directories and
    helper modules would make pytest collect nothing."""
    tasks = [
        {
            "id": "9",
            "description": (
                "## 修改位置\n"
                "- 新建测试 `backend/tests/integration/test_corpus_reader`\n"
                "- 新建健康语料模板 `backend/tests/fixtures/failure_miner/` 目录\n"
                "- 新建辅助模块 `backend/tests/e2e/failure_miner_helpers`"
            ),
            "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"],
        }
    ]
    out = gen._backfill_files_to_modify_from_description(tasks)
    cmd = out[0]["test_command"]
    assert "backend/tests/integration/test_corpus_reader.py" in cmd
    assert "fixtures" not in cmd
    assert "helpers" not in cmd


# ---------------------------------------------------------------------------
# 4. Missing test_command is synthesized from the TDD / 修改位置 sections
# ---------------------------------------------------------------------------


def test_backfill_synthesizes_missing_test_command(gen):
    tasks = [
        {
            "id": "5",
            "description": (
                "## 修改位置\n"
                "- 新建测试 `backend/tests/unit/test_provider_routing`\n"
                "- 新建测试 `backend/tests/unit/test_failure_miner_llm_client`\n"
                "## TDD 规格\n"
                "- `test_classify_protocol`: …（对应 tests/unit/test_llm_client）"
            ),
            "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"],
        }
    ]
    out = gen._backfill_files_to_modify_from_description(tasks)
    cmd = out[0]["test_command"]
    assert cmd.startswith("backend/.venv/bin/python3 -m pytest")
    assert "backend/tests/unit/test_provider_routing.py" in cmd
    assert "backend/tests/unit/test_failure_miner_llm_client.py" in cmd
    # The bare "（对应 tests/…）" cross-reference inside TDD 规格 is a
    # pointer, not a deliverable path — extraction is scoped to 修改位置.
    assert "tests/unit/test_llm_client.py" not in cmd
    assert cmd.endswith(" -v")


def test_backfill_does_not_overwrite_existing_test_command(gen):
    tasks = [
        {
            "id": "5",
            "description": "- 新建测试 `backend/tests/unit/test_x`",
            "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"],
            "test_command": "pytest tests/unit/test_custom.py -v",
        }
    ]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert out[0]["test_command"] == "pytest tests/unit/test_custom.py -v"


def test_backfill_ignores_directory_mentions_for_test_command(gen):
    tasks = [
        {
            "id": "9",
            "description": "新建健康语料模板 `backend/tests/fixtures/failure_miner/` 目录",
            "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"],
        }
    ]
    out = gen._backfill_files_to_modify_from_description(tasks)
    assert out[0].get("test_command") in (None, "")


# ---------------------------------------------------------------------------
# 5. Semantic package dependencies are wired, cycle-guarded
# ---------------------------------------------------------------------------


def test_infer_package_dependencies_wires_package_consumers(gen):
    tasks = [
        {
            "id": "1",
            "description": (
                "建立包骨架\n## 修改位置\n"
                "- 新建包目录 `backend/failure_miner/`，含包初始化模块"
            ),
            "files_to_modify": [
                "backend/failure_miner/__init__.py",
                "backend/failure_miner/models.py",
            ],
            "depends_on": [],
        },
        {
            "id": "2",
            "description": "新建模块",
            "files_to_modify": ["backend/failure_miner/normalizer.py"],
            "depends_on": [],
        },
        {
            "id": "3",
            "description": "无关任务",
            "files_to_modify": ["backend/server.py"],
            "depends_on": [],
        },
    ]
    out = gen._infer_package_dependencies(tasks)
    by_id = {t["id"]: t for t in out}
    assert "1" in by_id["2"]["depends_on"], (
        "a module created inside the package must depend on the task "
        "that creates the package"
    )
    assert "1" not in by_id["3"]["depends_on"]
    # existing edges are preserved
    assert by_id["1"]["depends_on"] == []


def test_infer_package_dependencies_cycle_guard(gen):
    tasks = [
        {
            "id": "1",
            "description": "新建包目录 `backend/failure_miner/`",
            "files_to_modify": ["backend/failure_miner/__init__.py"],
            "depends_on": ["2"],
        },
        {
            "id": "2",
            "description": "x",
            "files_to_modify": ["backend/failure_miner/normalizer.py"],
            "depends_on": [],
        },
    ]
    out = gen._infer_package_dependencies(tasks)
    by_id = {t["id"]: t for t in out}
    # Adding 1 → 2's depends_on would close the cycle 1 → 2 → 1.
    assert by_id["2"]["depends_on"] == []


def test_infer_package_dependencies_no_providers_is_noop(gen):
    tasks = [
        {
            "id": "1",
            "description": "x",
            "files_to_modify": ["backend/server.py"],
            "depends_on": [],
        }
    ]
    out = gen._infer_package_dependencies(tasks)
    assert out[0]["depends_on"] == []


def test_infer_package_dependencies_uses_description_module_paths(gen):
    """Module paths that fail the pass-2b parent guard (the package does
    not exist yet at generation time) never reach ``files_to_modify`` —
    but the dependency "my module lives inside that package" is real and
    must be inferred from the description, or tasks 2/3/4-style modules
    lose their edge to the package-skeleton task."""
    tasks = [
        {
            "id": "1",
            "description": (
                "建立包骨架\n## 修改位置\n"
                "- 新建包目录 `backend/failure_miner/`，含包初始化模块"
            ),
            "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"],
            "depends_on": [],
        },
        {
            "id": "2",
            "description": "## 修改位置\n- 新建模块 `backend/failure_miner/normalizer`",
            "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"],
            "depends_on": [],
        },
        {
            "id": "3",
            "description": "无关任务，不提任何包内路径",
            "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"],
            "depends_on": [],
        },
    ]
    out = gen._infer_package_dependencies(tasks)
    by_id = {t["id"]: t for t in out}
    assert "1" in by_id["2"]["depends_on"]
    assert "1" not in by_id["3"]["depends_on"]
    assert by_id["1"]["depends_on"] == []


# ---------------------------------------------------------------------------
# 6. The generation prompt must state the new contracts
# ---------------------------------------------------------------------------


def test_generation_prompt_requires_decision_point_citations():
    import tasks_generator

    assert "## 对应上游决策点（CRITICAL" in tasks_generator.TASKS_SYSTEM_PROMPT
    assert (
        "arch 决策点 N" in tasks_generator.TASKS_SYSTEM_PROMPT
        and "test 决策点 N" in tasks_generator.TASKS_SYSTEM_PROMPT
    )


def test_generation_prompt_requires_package_ownership_dependency():
    import tasks_generator

    assert (
        "新建模块若位于某任务创建的包/目录内"
        in tasks_generator.TASKS_SYSTEM_PROMPT
    )
