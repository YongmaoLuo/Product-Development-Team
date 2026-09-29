"""
Integration test: workspace_utils + tasks_generator.validate_workspace.

Tests the full flow:
1. workspace_utils.detect_venv finds the project's own interpreter
2. TasksGenerator._rewrite_cmd correctly replaces old path with new path
3. TasksGenerator.validate_workspace (with MockCodingTool) assigns each
   task to the right workspace and rewrites commands accordingly.
"""
import os
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional

# Add backend dir to import path
BACKEND_DIR = Path(__file__).resolve().parents[1] \
    if not os.environ.get("PDT_DEV_REPO") \
    else Path(os.environ["PDT_DEV_REPO"]) / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from tasks_generator import TasksGenerator
import workspace_utils


class TestRewriteCmd(unittest.TestCase):
    """TasksGenerator._rewrite_cmd swaps old project path for new one."""

    def test_simple_path_replace(self):
        result = TasksGenerator._rewrite_cmd(
            "cd /old/path && cargo test",
            "/old/path",
            "/new/path",
        )
        self.assertEqual(result, "cd /new/path && cargo test")

    def test_no_match_unchanged(self):
        result = TasksGenerator._rewrite_cmd(
            "cd /other/path && cargo test",
            "/old/path",
            "/new/path",
        )
        self.assertEqual(result, "cd /other/path && cargo test")

    def test_empty_old_dir(self):
        result = TasksGenerator._rewrite_cmd(
            "cd /some/path && cargo test",
            "",
            "/new/path",
        )
        self.assertEqual(result, "cd /some/path && cargo test")

    def test_same_dir_no_op(self):
        result = TasksGenerator._rewrite_cmd(
            "cd /path && cargo test",
            "/path",
            "/path",
        )
        self.assertEqual(result, "cd /path && cargo test")


class MockCodingTool:
    """Mock that records prompts and returns canned responses based on content."""

    def __init__(self, response_per_call: Optional[List[dict]] = None):
        self.queries: List[dict] = []
        self._response_iter = iter(response_per_call or [])
        self._default_response = {"project_dir": None, "reason": "no change"}

    def query(self, prompt: str, system_instruction: Optional[str] = None,
              retries: int = 3, timeout: Optional[int] = None) -> str:
        self.queries.append({"method": "query", "prompt": prompt})
        return "{}"

    def query_json(self, prompt: str, system_instruction: Optional[str] = None,
                   retries: int = 3, timeout: Optional[int] = None) -> dict:
        self.queries.append({"method": "query_json", "prompt": prompt})
        try:
            return next(self._response_iter)
        except StopIteration:
            return self._default_response


class TestDeclaredWorkspaces(unittest.TestCase):
    """The plan's own declaration — now the ONLY input to workspace routing.

    Rewritten 2026-09-22. The previous ``TestValidateWorkspace`` pinned the
    candidate-scan + LLM-router behaviour: scan the user's home for marker
    files, hand the hits to an LLM, rewrite any path with a ``-dev``
    sibling. That is what was removed — it is meaningful on exactly one
    machine, and on a production plan it put all 14
    tasks on the production checkout. The full contract now lives in
    ``tests/contract/test_workspace_declared_not_guessed.py``; this class
    covers the declaration readers underneath it.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ws-declared-")).resolve()
        # ``.resolve()`` matters on macOS: ``/var`` is a symlink to
        # ``/private/var`` and every declared path is resolved before use.
        self.plan_dir = self.tmpdir / "plan"
        self.plan_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _gen(self, mock=None) -> TasksGenerator:
        return TasksGenerator(mock or MockCodingTool(), self.plan_dir)

    def _write_prd(self, constraints) -> None:
        (self.plan_dir / "prd.json").write_text(
            json.dumps({"constraints": constraints}), encoding="utf-8"
        )

    def _write_interview(self, constraints) -> None:
        (self.plan_dir / "interview.json").write_text(
            json.dumps({"dimensions": {"constraints": constraints}}),
            encoding="utf-8",
        )

    def test_no_declaration_yields_empty(self):
        self.assertEqual(self._gen()._declared_workspace_dirs(), [])

    def test_dict_constraints_are_resolved(self):
        proj = self.tmpdir / "proj"
        proj.mkdir()
        self._write_prd({"target_project_dir": str(proj)})
        self.assertEqual(self._gen()._declared_workspace_dirs(), [str(proj)])

    def test_semicolon_string_constraints(self):
        proj = self.tmpdir / "proj"
        proj.mkdir()
        self._write_prd(f"target_project_dir={proj}; deadline=none")
        self.assertEqual(self._gen()._declared_workspace_dirs(), [str(proj)])

    def test_duplicate_keys_collapse(self):
        proj = self.tmpdir / "proj"
        proj.mkdir()
        self._write_prd(
            {"target_project_dir": str(proj), "primary_project_dir": str(proj)}
        )
        self.assertEqual(self._gen()._declared_workspace_dirs(), [str(proj)])

    def test_interview_is_the_fallback_source(self):
        proj = self.tmpdir / "proj"
        proj.mkdir()
        self._write_interview({"primary_project_dir": str(proj)})
        self.assertEqual(self._gen()._declared_workspace_dirs(), [str(proj)])

    def test_workspace_context_lists_every_declared_dir(self):
        a = self.tmpdir / "a"
        b = self.tmpdir / "b"
        a.mkdir()
        b.mkdir()
        self._write_prd(
            {"primary_project_dir": str(a), "secondary_project_dir": str(b)}
        )
        context = self._gen()._load_workspace_context()
        self.assertIn(str(a), context)
        self.assertIn(str(b), context)
        self.assertIn("primary_project_dir", context)

    def test_workspace_context_is_none_without_a_declaration(self):
        """``None`` is deliberate — it is what tells the prompt builder to
        omit the ``project_dir`` demand entirely."""
        self.assertIsNone(self._gen()._load_workspace_context())

    def test_validate_workspace_never_calls_the_llm(self):
        """Guessing required an LLM. Declaring does not — so a plan with no
        declaration must not spend a token on workspace routing."""
        mock = MockCodingTool()
        gen = self._gen(mock)
        gen.validate_workspace({"requirement": "r", "tasks": []})
        self.assertEqual(mock.queries, [])


class TestDetectVenv(unittest.TestCase):
    """workspace_utils.detect_venv finds the right venv for a Python project."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ws-venv-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_venv1_wins_over_venv(self):
        # the project convention: venv1/ should be preferred over venv/
        proj = self.tmpdir / "proj"
        proj.mkdir()
        (proj / "venv1" / "bin").mkdir(parents=True)
        (proj / "venv1" / "bin" / "activate").write_text("# fake")
        (proj / "venv" / "bin").mkdir(parents=True)
        (proj / "venv" / "bin" / "activate").write_text("# fake")
        result = workspace_utils.detect_venv(proj)
        self.assertIn("venv1", result)

    def test_venv_in_parent_dir(self):
        # Project doesn't have venv, parent does
        proj = self.tmpdir / "proj"
        proj.mkdir()
        (self.tmpdir / "venv1" / "bin").mkdir(parents=True)
        (self.tmpdir / "venv1" / "bin" / "activate").write_text("# fake")
        result = workspace_utils.detect_venv(proj)
        self.assertIn("venv1", result)

    def test_no_venv_returns_none(self):
        proj = self.tmpdir / "proj"
        proj.mkdir()
        result = workspace_utils.detect_venv(proj)
        self.assertIsNone(result)


class TestNeedsVenv(unittest.TestCase):
    """TasksGenerator._needs_venv detects Python commands needing venv."""

    def test_pytest_needs_venv(self):
        self.assertTrue(TasksGenerator._needs_venv("pytest tests/test_foo.py -v"))

    def test_python_needs_venv(self):
        self.assertTrue(TasksGenerator._needs_venv("python tests/test_foo.py"))

    def test_pip_install_needs_venv(self):
        self.assertTrue(TasksGenerator._needs_venv("pip install -r requirements.txt"))

    def test_cargo_does_not_need_venv(self):
        self.assertFalse(TasksGenerator._needs_venv("cargo test --lib"))

    def test_already_source_skipped(self):
        self.assertFalse(TasksGenerator._needs_venv("source venv/bin/activate && pytest tests/"))

    def test_already_conda_activate_skipped(self):
        self.assertFalse(TasksGenerator._needs_venv("conda activate myenv && pytest tests/"))

    def test_already_poetry_run_skipped(self):
        self.assertFalse(TasksGenerator._needs_venv("poetry run pytest tests/"))

    def test_already_uv_run_skipped(self):
        self.assertFalse(TasksGenerator._needs_venv("uv run pytest tests/"))


class TestRewriteCmdWithVenv(unittest.TestCase):
    """_rewrite_cmd injects venv prefix for Python commands."""

    def test_pytest_gets_venv_injected(self):
        result = TasksGenerator._rewrite_cmd(
            "cd /new/path && pytest tests/test_foo.py -v",
            "/old/path", "/new/path",
            venv_path="/new/path/venv1/bin/activate",
        )
        self.assertEqual(
            result,
            "source /new/path/venv1/bin/activate && cd /new/path && pytest tests/test_foo.py -v",
        )

    def test_cargo_no_venv(self):
        result = TasksGenerator._rewrite_cmd(
            "cd /new/path && cargo test --lib",
            "/old/path", "/new/path",
            venv_path="/new/path/venv1/bin/activate",
        )
        # Should NOT add venv prefix (cargo is not Python)
        self.assertNotIn("source", result)
        self.assertEqual(result, "cd /new/path && cargo test --lib")

    def test_already_activated_not_double_prefixed(self):
        result = TasksGenerator._rewrite_cmd(
            "source venv1/bin/activate && pytest tests/test_foo.py",
            "/old/path", "/new/path",
            venv_path="/new/path/venv1/bin/activate",
        )
        # Should NOT add another source prefix
        self.assertEqual(result.count("source"), 1)

    def test_no_venv_path_no_injection(self):
        result = TasksGenerator._rewrite_cmd(
            "cd /new/path && pytest tests/test_foo.py -v",
            "/old/path", "/new/path",
            venv_path=None,
        )
        # No venv → no source prefix added
        self.assertNotIn("source", result)


class TestValidateWorkspaceVenv(unittest.TestCase):
    """The declared workspace's own virtualenv is wired into test commands.

    Not a routing decision — the directory is already fixed by the plan's
    declaration. A bare ``pytest`` in a project that ships ``venv1/``
    resolves some other interpreter, so the subagent and the framework's
    independent re-run disagree about whether the command passed.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ws-validate-venv-")).resolve()
        self.plan_dir = self.tmpdir / "plan"
        self.plan_dir.mkdir()
        self.py_proj = self.tmpdir / "real" / "py-app"
        self.py_proj.mkdir(parents=True)
        (self.py_proj / "setup.py").write_text("from setuptools import setup")
        (self.py_proj / "venv1" / "bin").mkdir(parents=True)
        (self.py_proj / "venv1" / "bin" / "activate").write_text("# fake")
        (self.plan_dir / "prd.json").write_text(
            json.dumps({"constraints": {"target_project_dir": str(self.py_proj)}}),
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_python_task_gets_the_declared_projects_venv(self):
        gen = TasksGenerator(MockCodingTool(), self.plan_dir)
        result = gen.validate_workspace(
            {
                "requirement": "test",
                "tasks": [
                    {
                        "id": "1",
                        "title": "Python 适配测试",
                        "description": "pytest tests/test_foo.py -v",
                        "project_dir": "/old/wrong",
                        "test_commands": ["pytest tests/test_foo.py -v"],
                    }
                ],
            },
        )
        task = result["tasks"][0]
        self.assertEqual(task["project_dir"], str(self.py_proj))
        self.assertIn("source", task["test_commands"][0])
        self.assertIn("venv1", task["test_commands"][0])
        self.assertTrue(task.get("_venv_injected"))

    def test_foreign_runner_is_rewritten_onto_the_projects_venv(self):
        """The 2026-09-21 ``uv run pytest`` defect, pinned."""
        gen = TasksGenerator(MockCodingTool(), self.plan_dir)
        result = gen.validate_workspace(
            {
                "requirement": "test",
                "tasks": [
                    {
                        "id": "1",
                        "title": "Python 适配测试",
                        "description": "uv run pytest tests/test_foo.py -v",
                        "project_dir": str(self.py_proj),
                        "test_commands": ["uv run pytest tests/test_foo.py -v"],
                    }
                ],
            },
        )
        cmd = result["tasks"][0]["test_commands"][0]
        self.assertNotIn("uv run", cmd)
        self.assertIn("venv1", cmd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
