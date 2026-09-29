"""
Unit tests for TasksGenerator.validate_workspace.

Rewritten 2026-09-22: ``validate_workspace`` no longer scans the filesystem
or asks an LLM to route tasks between candidate repos. It binds every task
to the workspace the PLAN declared, and refuses the checkout running the backend server
server. See ``tests/contract/test_workspace_declared_not_guessed.py`` for
the full contract.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


class _MockCodingTool:
    """Records prompts and returns canned JSON responses."""

    def __init__(self, response_per_call=None):
        self.queries: list = []
        self._response_iter = iter(response_per_call or [])
        self._default_response = {"project_dir": None, "reason": "no change"}

    def query_json(self, prompt: str, system_instruction: str = "", **kwargs) -> dict:
        self.queries.append({"prompt": prompt, "system_instruction": system_instruction})
        try:
            return next(self._response_iter)
        except StopIteration:
            return self._default_response


class TestValidateWorkspaceDeclaredTarget(unittest.TestCase):
    """The plan's declared workspace is the authority; nothing is guessed.

    2026-09-22 — rewritten. The two classes that used to live here pinned
    mechanisms that were removed:

      * ``TestValidateWorkspaceFormalDevRepo`` — an LLM candidate router
        whose prompt asked the model to classify "正式仓库" vs "开发仓库"
        and pick the dev one;
      * the ``-dev`` sibling rewrite that ran before and after it.

    Both read the user's local layout, which is meaningful on exactly one
    machine. On a production plan — which declared
    nothing — that machinery put all 14 tasks on the production checkout.
    The full contract now lives in
    ``tests/contract/test_workspace_declared_not_guessed.py``; this class
    keeps the end-to-end path exercised through a real plan directory.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ws-hard-target-")).resolve()
        self.plan_dir = self.tmpdir / "plan"
        self.plan_dir.mkdir()

        # The declared target — deliberately marker-free (no setup.py /
        # pyproject.toml / Cargo.toml). Under the old code such a directory
        # was invisible to the filesystem scan and only survived because of
        # the post-hoc override; now it is simply what the plan said.
        self.user_target = self.tmpdir / "isolated-sandbox"
        self.user_target.mkdir()
        (self.user_target / "README.md").write_text("# sandbox")

        # The checkout running the backend server. Tasks must never land here.
        self.formal_repo = self.tmpdir / "formal-ac"
        self.formal_repo.mkdir()
        (self.formal_repo / "setup.py").write_text("from setuptools import setup")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_prd(self, target_dir: str) -> None:
        prd = {
            "title": "smoke",
            "overview": "x",
            "constraints": {
                "tech_stack": "Python",
                "target_project_dir": target_dir,
            },
            "acceptance": ["pytest tests/ 全部通过"],
        }
        (self.plan_dir / "prd.json").write_text(
            json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _make_gen(self):
        from tasks_generator import TasksGenerator

        return TasksGenerator(_MockCodingTool(), self.plan_dir)

    def test_declared_target_overrides_a_formal_repo_path(self):
        """Smoke v3: the LLM emitted the formal repo as project_dir. The
        plan's declaration must win."""
        self._write_prd(str(self.user_target))
        gen = self._make_gen()
        tasks_data = {
            "requirement": "smoke",
            "tasks": [
                {
                    "id": "1",
                    "title": "在 src/calculator.py 中新增 subtract",
                    "project_dir": str(self.formal_repo),
                },
                {
                    "id": "2",
                    "title": "在 tests/test_calculator.py 中新增测试",
                    "project_dir": str(self.formal_repo),
                },
            ],
        }
        result = gen.validate_workspace(
            tasks_data, formal_repo_dir=self.formal_repo
        )
        for task in result["tasks"]:
            self.assertEqual(
                Path(task["project_dir"]).resolve(),
                Path(str(self.user_target)).resolve(),
                f"task {task['id']} was not placed in the declared target",
            )
            self.assertTrue(task.get("_workspace_validated"))
            self.assertIn("declared workspace", task.get("_workspace_reason", ""))

    def test_off_target_path_snaps_to_the_declared_target(self):
        self._write_prd(str(self.user_target))
        gen = self._make_gen()
        wrong_dir = self.tmpdir / "some-other-project"
        wrong_dir.mkdir()
        (wrong_dir / "setup.py").write_text("from setuptools import setup")
        result = gen.validate_workspace(
            {
                "requirement": "smoke",
                "tasks": [{"id": "1", "title": "task 1", "project_dir": str(wrong_dir)}],
            },
            formal_repo_dir=self.formal_repo,
        )
        task = result["tasks"][0]
        self.assertEqual(
            Path(task["project_dir"]).resolve(),
            Path(str(self.user_target)).resolve(),
        )
        self.assertIn(str(wrong_dir), task["_workspace_reason"])

    def test_path_under_the_declared_target_is_preserved(self):
        """A subdirectory of the declared target keeps its sub-path — it is
        a real place inside the declared repo, not an off-target leak."""
        self._write_prd(str(self.user_target))
        gen = self._make_gen()
        sub_dir = self.user_target / "src"
        sub_dir.mkdir()
        result = gen.validate_workspace(
            {
                "requirement": "smoke",
                "tasks": [{"id": "1", "title": "task 1", "project_dir": str(sub_dir)}],
            },
            formal_repo_dir=self.formal_repo,
        )
        self.assertEqual(result["tasks"][0]["project_dir"], str(sub_dir))

    def test_no_declaration_clears_a_formal_repo_path_instead_of_guessing(self):
        """Without a declaration there is no target to snap to — and the one
        rule that is not a guess still holds: never target the checkout
        running the backend server. The path is cleared so the run's own
        project_dir (supplied at /start) governs."""
        (self.plan_dir / "prd.json").write_text(
            json.dumps({"constraints": {"tech_stack": "Python"}}), encoding="utf-8"
        )
        gen = self._make_gen()
        result = gen.validate_workspace(
            {
                "requirement": "smoke",
                "tasks": [{"id": "1", "title": "task 1", "project_dir": str(self.formal_repo)}],
            },
            formal_repo_dir=self.formal_repo,
        )
        task = result["tasks"][0]
        self.assertEqual(task["project_dir"], "")
        self.assertIn("refused formal-repo target", task["_workspace_reason"])
        self.assertNotIn(str(self.tmpdir), task["project_dir"])

    def test_target_from_interview_json_fallback(self):
        interview = {
            "dimensions": {
                "constraints": {"target_project_dir": str(self.user_target)}
            }
        }
        (self.plan_dir / "interview.json").write_text(
            json.dumps(interview, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._write_prd("")  # empty PRD target → falls through to interview
        gen = self._make_gen()
        result = gen.validate_workspace(
            {
                "requirement": "smoke",
                "tasks": [{"id": "1", "title": "task 1", "project_dir": str(self.formal_repo)}],
            },
            formal_repo_dir=self.formal_repo,
        )
        self.assertEqual(
            Path(result["tasks"][0]["project_dir"]).resolve(),
            Path(str(self.user_target)).resolve(),
        )

    def test_bare_project_dir_key_is_honored_as_target(self):
        """Smoke v4 live observation: the interview/PRD LLM writes the single
        target under the bare ``project_dir`` key (not ``target_project_dir``).
        It must still count as a declaration — otherwise the plan silently
        stops declaring anything and the tasks go unmanaged."""
        prd = {
            "title": "smoke",
            "overview": "x",
            "constraints": {
                "tech_stack": "Python",
                "project_dir": str(self.user_target),
            },
            "acceptance": ["pytest tests/ 全部通过"],
        }
        (self.plan_dir / "prd.json").write_text(
            json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        gen = self._make_gen()
        result = gen.validate_workspace(
            {
                "requirement": "smoke",
                "tasks": [
                    {"id": "1", "title": "task 1", "project_dir": str(self.formal_repo)},
                    {"id": "2", "title": "task 2", "project_dir": str(self.formal_repo)},
                ],
            },
            formal_repo_dir=self.formal_repo,
        )
        for task in result["tasks"]:
            self.assertEqual(
                Path(task["project_dir"]).resolve(),
                Path(str(self.user_target)).resolve(),
                f"task {task['id']}: bare project_dir key not honored as target",
            )

    def test_no_llm_call_is_made(self):
        """Routing used to cost an LLM round-trip. Declaring costs none."""
        self._write_prd(str(self.user_target))
        from tasks_generator import TasksGenerator

        mock = _MockCodingTool()
        gen = TasksGenerator(mock, self.plan_dir)
        gen.validate_workspace(
            {
                "requirement": "smoke",
                "tasks": [{"id": "1", "title": "task 1", "project_dir": str(self.formal_repo)}],
            },
            formal_repo_dir=self.formal_repo,
        )
        self.assertEqual(mock.queries, [])


if __name__ == "__main__":
    unittest.main()
