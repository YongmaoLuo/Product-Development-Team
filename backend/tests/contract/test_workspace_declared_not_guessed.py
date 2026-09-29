"""Contract: the working directory is DECLARED by the plan, never guessed.

2026-09-22
-------------------------
A fabricated workspace is the bug (2026-09-22): it may happen to work
on one machine and be nonsense on every other.

A rule that reads the local filesystem — ``~/work``,
``-dev`` siblings — is meaningful on exactly one machine. For an outside
user who clones this repo, the "adaptive" behaviour degrades into an
inexplicable one. So:

  * the plan declares a workspace → that declaration is the ONLY
    authority, and every task must land inside it;
  * the plan declares nothing → the framework does NOT guess. No
    filesystem scan, no LLM candidate router, no sibling-repo heuristic.
    ``POST /api/execution/{id}/start`` supplies the directory instead.

What this replaces, and why it mattered: the old path scanned the user's
home for marker files and handed the hits to an LLM router, then ran a
``map_to_dev_repo`` pass that rewrote any path having a ``-dev`` /
``-exec`` / ``-workspace`` sibling. On plan
a production plan — which declared nothing — that
machinery put all 14 tasks on the production checkout
``the production checkout``, because a parallel session had left
files there and it therefore looked like the live workspace.

The one guess-free rule that survives is the formal-repo exclusion: the
checkout running the backend server is never a target. That is a property of
the framework, not of the user's layout, so it holds on every machine —
and it is an exclusion, never a redirect to some sibling directory.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from tasks_generator import (  # noqa: E402
    MULTI_WORKSPACE_PROMPT_TEMPLATE,
    NO_WORKSPACE_PROMPT,
    TASKS_SYSTEM_PROMPT,
    TasksGenerationError,
    TasksGenerator,
    _declared_workspace_pairs,
)


class _NoLLM:
    """validate_workspace must not need an LLM at all any more."""

    def query(self, *a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("validate_workspace must not call the LLM")

    query_json = query


class TestDeclaredWorkspacePairs(unittest.TestCase):
    """The three constraint shapes the interview/PRD LLMs actually emit."""

    def test_dict_shape(self):
        pairs = _declared_workspace_pairs(
            {"primary_project_dir": "/a", "notes": "ignore me"}
        )
        self.assertEqual(pairs, [("primary_project_dir", "/a")])

    def test_semicolon_string_shape(self):
        pairs = _declared_workspace_pairs(
            "target_project_dir=/a; deadline=none; primary_project_dir=/b"
        )
        self.assertEqual(
            pairs, [("target_project_dir", "/a"), ("primary_project_dir", "/b")]
        )

    def test_list_of_prose_declares_nothing(self):
        """The 0921 PRD emitted a list of prose constraints — six sentences,
        no ``key=value`` anywhere. That is "declared nothing", and it must
        read as such rather than as a lookup failure."""
        constraints = [
            "技术栈事实（访谈已定，不在本项目内重新选型）：核心算法在 Rust 仓",
            "理论依据约束：所有分段必须能对应到规范原文第 17/20/24 课",
            "工期：访谈未提供明确 deadline。",
        ]
        self.assertEqual(_declared_workspace_pairs(constraints), [])

    def test_list_of_key_value_entries(self):
        pairs = _declared_workspace_pairs(
            ["target_project_dir=/a", "unrelated prose", {"primary_project_dir": "/b"}]
        )
        self.assertEqual(
            pairs, [("target_project_dir", "/a"), ("primary_project_dir", "/b")]
        )

    def test_target_beats_bare_project_dir(self):
        pairs = _declared_workspace_pairs(
            {"project_dir": "/b", "target_project_dir": "/a"}
        )
        self.assertEqual(pairs[0], ("target_project_dir", "/a"))

    def test_garbage_shapes_yield_nothing(self):
        for bad in (None, 42, {"nope": 1}, [], ""):
            self.assertEqual(_declared_workspace_pairs(bad), [])


class _ValidateHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ws-declared-")).resolve()
        # ``.resolve()`` matters on macOS: ``/var`` is a symlink to
        # ``/private/var`` and the implementation resolves every declared
        # path before comparing.
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        # A workspace that WOULD be discoverable by the old marker-file scan.
        # Nothing may select it, because the plan never named it.
        self.discoverable = self.tmp / "somewhere" / "proj"
        self.discoverable.mkdir(parents=True)
        (self.discoverable / "setup.py").write_text("from setuptools import setup")
        self.gen = TasksGenerator(_NoLLM(), self.plan_dir)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _declare(self, constraints) -> None:
        (self.plan_dir / "prd.json").write_text(
            json.dumps({"constraints": constraints}), encoding="utf-8"
        )

    @staticmethod
    def _tasks(*dirs) -> dict:
        return {
            "requirement": "r",
            "tasks": [
                {"id": str(i + 1), "title": f"t{i + 1}", "project_dir": d}
                for i, d in enumerate(dirs)
            ],
        }


class TestNoDeclarationMeansNoGuessing(_ValidateHarness):
    def test_tasks_are_left_alone(self):
        """No declaration → no scan, no rewrite. The tasks keep what they
        had, and the run's own project_dir governs at execution time."""
        data = self._tasks("", "/some/other/place")
        result = self.gen.validate_workspace(data, search_dirs=[self.tmp / "somewhere"])
        self.assertEqual(result["tasks"][0]["project_dir"], "")
        self.assertEqual(result["tasks"][1]["project_dir"], "/some/other/place")
        self.assertNotEqual(
            result["tasks"][1]["project_dir"],
            str(self.discoverable),
            "a discoverable repo on disk must not be picked",
        )

    def test_list_shaped_constraints_are_not_a_declaration(self):
        self._declare(["工期：访谈未提供明确 deadline。"])
        data = self._tasks("/some/other/place")
        result = self.gen.validate_workspace(data)
        self.assertEqual(result["tasks"][0]["project_dir"], "/some/other/place")

    def test_formal_repo_target_is_refused(self):
        """The one rule that is not a guess: never write into the checkout
        running the backend server. It is cleared, not redirected somewhere."""
        formal = self.tmp / "ac-formal"
        target = formal / "backend" / "x.py"
        data = self._tasks(str(target.parent))
        result = self.gen.validate_workspace(data, formal_repo_dir=formal)
        task = result["tasks"][0]
        self.assertEqual(task["project_dir"], "")
        self.assertIn("refused formal-repo target", task["_workspace_reason"])


class TestDeclarationIsTheAuthority(_ValidateHarness):
    def test_single_declared_workspace_owns_every_task(self):
        declared = self.tmp / "declared"
        declared.mkdir()
        self._declare({"target_project_dir": str(declared)})
        data = self._tasks("/old/wrong", f"{declared}/already/inside")
        result = self.gen.validate_workspace(data)
        self.assertEqual(result["tasks"][0]["project_dir"], str(declared))
        # A task already inside the declared tree keeps its sub-path.
        self.assertEqual(
            result["tasks"][1]["project_dir"], f"{declared}/already/inside"
        )

    def test_task_outside_a_multi_workspace_declaration_fails_generation(self):
        a = self.tmp / "a"
        b = self.tmp / "b"
        a.mkdir()
        b.mkdir()
        self._declare(
            {"primary_project_dir": str(a), "secondary_project_dir": str(b)}
        )
        data = self._tasks(str(a), str(self.tmp / "elsewhere"))
        with self.assertRaises(TasksGenerationError) as ctx:
            self.gen.validate_workspace(data)
        payload = ctx.exception.error_payload
        self.assertEqual(payload["failures"], [{"id": "2", "project_dir": str(self.tmp / "elsewhere")}])
        self.assertEqual(payload["declared_workspaces"], [str(a), str(b)])

    def test_multi_workspace_tasks_inside_are_kept(self):
        a = self.tmp / "a"
        b = self.tmp / "b"
        a.mkdir()
        b.mkdir()
        self._declare(
            {"primary_project_dir": str(a), "secondary_project_dir": str(b)}
        )
        data = self._tasks(str(a / "sub"), str(b))
        result = self.gen.validate_workspace(data)
        self.assertEqual(result["tasks"][0]["project_dir"], str(a / "sub"))
        self.assertEqual(result["tasks"][1]["project_dir"], str(b))

    def test_missing_project_dir_defaults_to_the_primary_declared_one(self):
        a = self.tmp / "a"
        b = self.tmp / "b"
        a.mkdir()
        b.mkdir()
        self._declare(
            {"primary_project_dir": str(a), "secondary_project_dir": str(b)}
        )
        data = self._tasks("")
        result = self.gen.validate_workspace(data)
        self.assertEqual(result["tasks"][0]["project_dir"], str(a))
        self.assertIn("primary declared workspace", result["tasks"][0]["_workspace_reason"])


class TestSystemPromptAsksOnlyWhatThePlanDeclared(unittest.TestCase):
    def test_no_never_substituted_placeholders(self):
        self.assertNotIn("{primary_project_dir}", TASKS_SYSTEM_PROMPT)
        self.assertNotIn("{secondary_project_dir}", TASKS_SYSTEM_PROMPT)

    def test_no_multi_workspace_demand_in_the_base_prompt(self):
        """The base prompt must not demand ``project_dir`` — a plan that
        declared nothing has no basis for one, and asking anyway is how a
        fabricated path gets into tasks.json."""
        self.assertNotIn("多工作区支持", TASKS_SYSTEM_PROMPT)
        self.assertNotIn("MUST 为每个任务指定 `project_dir`", TASKS_SYSTEM_PROMPT)

    def test_template_takes_the_declared_list(self):
        rendered = MULTI_WORKSPACE_PROMPT_TEMPLATE.format(
            workspaces="- `/x/dev-checkout`"
        )
        self.assertIn("/x/dev-checkout", rendered)
        self.assertIn("不要自己编路径", rendered)

    def test_no_declaration_variant_forbids_the_field(self):
        self.assertIn("不要输出 `project_dir` 字段", NO_WORKSPACE_PROMPT)


class TestStartAdoptsTheRunProjectDir(unittest.TestCase):
    """A plan that declared nothing follows the dir ``/start`` was given.

    This is the second half of the contract. At generation time the operator
    has stated nothing yet, so a declared-nothing plan's tasks carry no
    ``project_dir``. ``POST /api/execution/{id}/start`` is therefore the
    first and only moment the framework learns the working directory — and
    the framework must use it rather than fall back on a guess.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ws-adopt-")).resolve()
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        self.tasks_file = self.plan_dir / "tasks.json"
        self.project = self.tmp / "run-project"
        self.project.mkdir()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_tasks(self, *dirs) -> None:
        self.tasks_file.write_text(
            json.dumps(
                {
                    "requirement": "r",
                    "tasks": [
                        {"id": str(i + 1), "title": f"t{i + 1}", "project_dir": d}
                        for i, d in enumerate(dirs)
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _read_tasks(self) -> dict:
        return json.loads(self.tasks_file.read_text(encoding="utf-8"))

    def _declare(self, constraints) -> None:
        (self.plan_dir / "prd.json").write_text(
            json.dumps({"constraints": constraints}), encoding="utf-8"
        )

    def test_no_declaration_repoints_every_task(self):
        from server import _adopt_run_project_dir

        self._write_tasks("/guessed/one", "", "/guessed/two")
        changed = _adopt_run_project_dir(
            self.plan_dir, self.project, self.tasks_file
        )
        self.assertEqual(changed, 3)
        for task in self._read_tasks()["tasks"]:
            self.assertEqual(task["project_dir"], str(self.project))
            self.assertIn("started with", task["_workspace_reason"])

    def test_a_declared_plan_is_left_untouched(self):
        from server import _adopt_run_project_dir

        declared = self.tmp / "declared"
        declared.mkdir()
        self._declare({"target_project_dir": str(declared)})
        self._write_tasks(str(declared))
        changed = _adopt_run_project_dir(
            self.plan_dir, self.project, self.tasks_file
        )
        self.assertIsNone(changed)
        self.assertEqual(
            self._read_tasks()["tasks"][0]["project_dir"], str(declared)
        )

    def test_missing_tasks_file_is_not_an_error(self):
        from server import _adopt_run_project_dir

        self.assertIsNone(
            _adopt_run_project_dir(self.plan_dir, self.project, self.tasks_file)
        )

    def test_corrupt_tasks_file_degrades_to_leaving_it_alone(self):
        from server import _adopt_run_project_dir

        self.tasks_file.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(
            _adopt_run_project_dir(self.plan_dir, self.project, self.tasks_file)
        )
        self.assertEqual(self.tasks_file.read_text(encoding="utf-8"), "{ not json")


if __name__ == "__main__":
    unittest.main()
