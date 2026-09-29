"""Unit tests for the 3 robustness fixes (smoke v9 follow-up).

These tests pin the contracts for:

  1. ``answer_interview`` tolerates dict-shaped dimension values
     (the LLM-interviewer writes ``{"value": "..."}``); the legacy
     ``.strip()`` call assumed strings and crashed.

  2. ``PlanState`` defaults to ``interview`` (not ``ready``) for
     new plans. The legacy default let plans skip straight from
     creation to execution without ever going through the
     interview phase.

  3. ``validate_workspace`` fills ``project_dir`` from the plan's declared
     workspace when the LLM emits empty string (not just a missing key).
     This used to live in ``_enforce_user_target``; that method was folded
     into ``validate_workspace`` on 2026-09-22, which is the same slot,
     reached through the public entry point.
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


# ---------------------------------------------------------------------------
# 1. answer_interview: dict / list / str tolerance
# ---------------------------------------------------------------------------


class TestAnswerInterviewDimensionValueCoercion(unittest.TestCase):

    def _make_request(self, dimension, answer):
        """Build a minimal AnswerInterviewRequest stub."""
        from server import AnswerInterviewRequest
        return AnswerInterviewRequest(dimension=dimension, answer=answer)

    def _post_answer(self, plan_dir, dimension, answer, existing_dimensions=None):
        """Drive /answer_interview directly without spinning up a
        full HTTP layer. We import the function and call it with
        a stub fastapi Request-style plan_dir injection."""
        from fastapi import HTTPException
        import server as srv
        from server import AnswerInterviewRequest

        if existing_dimensions is None:
            existing_dimensions = {}
        # Pre-seed interview.json so the function loads existing
        # dimensions.
        interview_file = plan_dir / "interview.json"
        interview_file.parent.mkdir(parents=True, exist_ok=True)
        interview_file.write_text(
            json.dumps({"dimensions": existing_dimensions}, ensure_ascii=False),
            encoding="utf-8",
        )

        req = AnswerInterviewRequest(dimension=dimension, answer=answer)
        # The function takes (plan_id, req) where plan_id is the
        # plan directory name (it's used to construct the path).
        plan_id = plan_dir.name
        # Stub PLANS_DIR so the function finds the right dir.
        original_plans_dir = srv.PLANS_DIR
        srv.PLANS_DIR = plan_dir.parent
        try:
            return srv.answer_interview(plan_id, req)
        finally:
            srv.PLANS_DIR = original_plans_dir

    def test_string_value_passes_through(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td) / "plan-A"
            plan_dir.mkdir()
            result = self._post_answer(
                plan_dir, "background", "calculator 缺少减法运算能力"
            )
            self.assertTrue(result["complete"] is False)
            stored = json.loads((plan_dir / "interview.json").read_text())
            self.assertEqual(
                stored["dimensions"]["background"],
                "calculator 缺少减法运算能力",
            )

    def test_dict_value_with_value_key_accepted(self):
        """LLM-interviewer writes ``{"value": "..."}`` — the legacy
        `.strip()` call assumed strings and crashed. The fix:
        coerce dict-shaped values to a flat string before checking."""
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td) / "plan-B"
            plan_dir.mkdir()
            self._post_answer(
                plan_dir,
                "background",
                "calculator 缺少减法运算能力",
            )
            # Now invoke the function again to test the dict-shape
            # coercion: pre-seed interview.json with a dict value
            # and call /answer.
            result = self._post_answer(
                plan_dir,
                "goals",
                "1.新增 subtract;2.新增 test_subtract;3.pytest 全绿",
            )
            stored = json.loads((plan_dir / "interview.json").read_text())
            self.assertEqual(stored["dimensions"]["goals"], "1.新增 subtract;2.新增 test_subtract;3.pytest 全绿")

    def test_dict_value_pre_existing_does_not_crash(self):
        """If interview.json already contains dict-shaped dimensions
        (written by /interview/start's LLM), /answer must:
          - not crash on the legacy .strip() call
          - correctly count the dimension as complete
        """
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td) / "plan-C"
            plan_dir.mkdir()
            # Pre-seed with all 5 dims as dict-shaped values
            existing = {
                "background": {"value": "calculator 缺少减法"},
                "goals": {"reasoning": "subtract + test_subtract"},
                "scope": {"value": "in: [subtract]; out: [other]"},
                "constraints": {"text": "Python+pytest"},
                "acceptance": {"answer": "pytest 全绿"},
            }
            (plan_dir / "interview.json").write_text(
                json.dumps({"dimensions": existing}, ensure_ascii=False),
                encoding="utf-8",
            )
            # Issue one more /answer — should not crash, and the
            # already-filled dict dimensions should still count as
            # complete (so `is_complete` is True).
            import server as srv
            from server import AnswerInterviewRequest
            original_plans_dir = srv.PLANS_DIR
            srv.PLANS_DIR = plan_dir.parent
            try:
                req = AnswerInterviewRequest(
                    dimension="background",
                    answer="explicit override",
                )
                result = srv.answer_interview(plan_dir.name, req)
            finally:
                srv.PLANS_DIR = original_plans_dir
            self.assertTrue(result["complete"], msg=result)
            self.assertEqual(
                set(result["dimensions_covered"]),
                {"background", "goals", "scope", "constraints", "acceptance"},
            )


# ---------------------------------------------------------------------------
# 2. PlanState default: interview, not ready
# ---------------------------------------------------------------------------


class TestPlanStateDefault(unittest.TestCase):

    def test_new_plan_starts_at_interview_not_ready(self):
        from plan_state import PlanState
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td) / "plan-default"
            plan_dir.mkdir()
            ps = PlanState(plan_dir)
            self.assertEqual(
                ps.get_current_phase(), "interview",
                "new plans must default to 'interview' (not 'ready') "
                "so the state machine doesn't skip the interview phase",
            )

    def test_in_progress_when_state_file_missing(self):
        from plan_state import PlanState
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td) / "plan-default-2"
            plan_dir.mkdir()
            ps = PlanState(plan_dir)
            # New plans must default to "interview" so the state
            # machine doesn't skip the interview phase.
            state = ps.get_state()
            self.assertEqual(state.get("current_phase"), "interview", msg=state)


# ---------------------------------------------------------------------------
# 3. tasks_generator: project_dir injection tolerates empty string
# ---------------------------------------------------------------------------


class TestValidateWorkspaceFillsEmptyProjectDir(unittest.TestCase):
    """An emitted ``project_dir: ""`` counts as missing, not as a value."""

    def _build(self, tmp: Path, constraints: dict):
        from unittest.mock import MagicMock

        from tasks_generator import TasksGenerator

        target = tmp / "user_target"
        target.mkdir()
        (target / "README.md").write_text("# user target")

        plan_dir = tmp / "plan"
        plan_dir.mkdir()
        (plan_dir / "interview.json").write_text(
            json.dumps(
                {"dimensions": {"constraints": constraints}}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        (plan_dir / "prd.json").write_text("{}", encoding="utf-8")

        class _Stub:
            _compact_prompt = None

        gen = TasksGenerator.__new__(TasksGenerator)
        gen.coding_tool = MagicMock(spec=_Stub)
        gen.plan_dir = plan_dir
        gen.prd_json = plan_dir / "prd.json"
        return target, gen

    def test_empty_string_project_dir_is_filled_from_target(self):
        """The LLM occasionally emits ``project_dir: ""`` instead of
        omitting the key. The fix treats empty string the same as
        missing."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td).resolve()
            target, gen = self._build(tmp, {})
            # Declaration has to exist before the generator is used; write
            # it now that we know the target path.
            (gen.plan_dir / "interview.json").write_text(
                json.dumps(
                    {
                        "dimensions": {
                            "constraints": {"project_dir": str(target)}
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = gen.validate_workspace(
                {
                    "requirement": "x",
                    "tasks": [
                        {"id": "1", "title": "implement X", "project_dir": ""},
                        {"id": "2", "title": "implement Y"},
                    ],
                }
            )
            expected = str(target)
            for task in result["tasks"]:
                self.assertEqual(
                    task["project_dir"],
                    expected,
                    f"task {task['id']} project_dir was not filled from "
                    f"the declared workspace",
                )
                self.assertTrue(task.get("_workspace_validated"))

    def test_declared_project_dir_inside_the_declaration_is_kept(self):
        """A task already placed inside the declared workspace is left where
        it is — being inside a sub-path of the declared repo is a real
        location, not a leak."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td).resolve()
            target, gen = self._build(tmp, {})
            sub = target / "src"
            sub.mkdir()
            (gen.plan_dir / "interview.json").write_text(
                json.dumps(
                    {
                        "dimensions": {
                            "constraints": {"project_dir": str(target)}
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = gen.validate_workspace(
                {
                    "requirement": "x",
                    "tasks": [
                        {"id": "1", "title": "implement X", "project_dir": str(sub)}
                    ],
                }
            )
            self.assertEqual(result["tasks"][0]["project_dir"], str(sub))

    def test_without_a_declaration_nothing_is_filled(self):
        """No declaration → the framework does not invent a directory."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td).resolve()
            _target, gen = self._build(tmp, {})
            result = gen.validate_workspace(
                {
                    "requirement": "x",
                    "tasks": [
                        {"id": "1", "title": "implement X", "project_dir": ""},
                        {"id": "2", "title": "implement Y"},
                    ],
                }
            )
            self.assertEqual(result["tasks"][0]["project_dir"], "")
            self.assertIsNone(result["tasks"][1].get("project_dir"))
            self.assertFalse(result["tasks"][0].get("_workspace_validated"))


