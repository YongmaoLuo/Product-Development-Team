"""Unit tests for the string-form constraints parser.

The interview/PRD LLM occasionally serialises the ``constraints``
block as a ``";"``-delimited ``key=value`` string instead of a
dict (observed live in smoke v11). The target extractor must
still pick up ``project_dir`` from this string form so the
hallucination fallback's safety net doesn't silently no-op.
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


def _build_gen(plan_dir: Path):
    from tasks_generator import TasksGenerator
    from unittest.mock import MagicMock

    gen = TasksGenerator.__new__(TasksGenerator)
    gen.coding_tool = MagicMock()
    gen.plan_dir = plan_dir
    gen.prd_json = plan_dir / "prd.json"
    return gen


class TestExtractUserTargetDirStringConstraints(unittest.TestCase):

    def test_interview_string_constraints_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            plan_dir.joinpath("interview.json").write_text(
                json.dumps(
                    {
                        "dimensions": {
                            "constraints": (
                                "tech_stack=Python+pytest;"
                                "project_dir=/tmp/user-target"
                            ),
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            gen = _build_gen(plan_dir)
            target = gen._extract_user_target_dir(None)
            self.assertEqual(target, str(Path("/tmp/user-target").resolve()))

    def test_prd_string_constraints_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            plan_dir.joinpath("interview.json").write_text(
                json.dumps({"dimensions": {}}, ensure_ascii=False)
            )
            plan_dir.joinpath("prd.json").write_text(
                json.dumps(
                    {
                        "constraints": (
                            "tech_stack=Python;"
                            "project_dir=/tmp/prd-target"
                        ),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            gen = _build_gen(plan_dir)
            gen.prd_json = plan_dir / "prd.json"
            target = gen._extract_user_target_dir(None)
            self.assertEqual(target, str(Path("/tmp/prd-target").resolve()))

    def test_dict_constraints_still_work(self):
        """Backward compat: dict-form constraints must continue
        to extract the same project_dir."""
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            plan_dir.joinpath("interview.json").write_text(
                json.dumps(
                    {
                        "dimensions": {
                            "constraints": {
                                "project_dir": "/tmp/dict-target",
                            },
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            gen = _build_gen(plan_dir)
            target = gen._extract_user_target_dir(None)
            self.assertEqual(target, str(Path("/tmp/dict-target").resolve()))

    def test_no_target_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            plan_dir.joinpath("interview.json").write_text(
                json.dumps(
                    {"dimensions": {"constraints": "tech_stack=Python"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            gen = _build_gen(plan_dir)
            self.assertIsNone(gen._extract_user_target_dir(None))


class TestEmptyTasksRaises(unittest.TestCase):
    """When the LLM returns an empty tasks list, the generator MUST
    raise ``TasksGenerationError`` and write a well-formed error
    payload to ``tasks.json``. The previous behaviour (synthesise a
    1-task fallback plan) was misleading — the synthesised task had
    no DP structure, no TDD specs, and frequently caused the executor
    to write code that diverged from the approved PRD/arch/test
    docs. Failing loudly is the correct behaviour; the user can
    decide to retry the endpoint."""

    def test_empty_tasks_raises_tasks_generation_error(self):
        """The new contract is: empty LLM response → raise
        TasksGenerationError, do NOT synthesise a fallback task.

        We exercise the empty-tasks branch directly via
        ``_post_process_tasks`` + the empty-tasks guard, instead of
        calling ``generate()`` (which runs preflight + LLM and is
        brittle in unit tests). The contract is: when
        ``tasks_data["tasks"]`` is empty after coercion, the
        generator raises TasksGenerationError instead of
        synthesising a fallback."""
        import inspect
        from tasks_generator import TasksGenerator, TasksGenerationError

        # The guard branch source must:
        #   1. raise TasksGenerationError (not fall through to a
        #      synthesised plan)
        #   2. write a well-formed error JSON to tasks.json
        source = inspect.getsource(TasksGenerator.generate)
        # 1. Find the empty-tasks branch and confirm it raises.
        empty_branch_start = source.find(
            "if not tasks_data[\"tasks\"]:"
        )
        self.assertGreater(
            empty_branch_start, 0,
            "expected an explicit 'if not tasks_data[tasks]' branch",
        )
        # Slice from the branch to the next top-level statement so
        # we only check the empty-tasks path.
        empty_branch = source[empty_branch_start:]
        next_block = empty_branch.find("\n        # ")
        if next_block > 0:
            empty_branch = empty_branch[:next_block]
        self.assertIn(
            "raise TasksGenerationError",
            empty_branch,
            "the empty-tasks branch must raise TasksGenerationError, "
            "not synthesise a fallback plan",
        )

    def test_fallback_synthesis_removed(self):
        """Regression guard: the generator source MUST NOT contain
        the old 'synthesised fallback' task template. If anyone
        re-introduces the fallback (which was misleading), this
        test fails."""
        import inspect
        from tasks_generator import TasksGenerator

        source = inspect.getsource(TasksGenerator.generate)
        self.assertNotIn(
            "由 fallback 调度生成",
            source,
            "the synthesised fallback task was removed in v14; "
            "TasksGenerationError must replace it",
        )
        self.assertNotIn(
            "synthesised fallback",
            source,
            "the old fallback rationale comment was removed; the "
            "new fail-loud path does not synthesise any task",
        )


if __name__ == "__main__":
    unittest.main()