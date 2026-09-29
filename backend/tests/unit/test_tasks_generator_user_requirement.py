"""Tests for the user-requirement pin at the top of the tasks prompt.

Smoke v12 surfaced a non-determinism where the LLM sometimes
hallucinated "no PRD provided" even with 12 KB+ of inlined
PRD/arch/test content. The fix pins the user-stated requirement
(extracted from interview.json's dimensions) as the leading
block of the prompt — the LLM cannot misread it as "missing".

This test pins the contract that:
  * Missing interview.json → returns None (no requirement block)
  * interview.json without dimensions → returns None
  * interview.json with dimensions → returns the JSON, capped at
    4000 chars + ellipsis
  * Malformed interview.json → returns None (graceful degrade)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _build_gen(plan_dir: Path):
    from tasks_generator import TasksGenerator

    gen = TasksGenerator.__new__(TasksGenerator)
    gen.coding_tool = MagicMock()
    gen.plan_dir = plan_dir
    gen.prd_json = plan_dir / "prd.json"
    gen.prd_md = plan_dir / "prd.md"
    return gen


class TestExtractUserRequirement(unittest.TestCase):
    def test_missing_interview_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            gen = _build_gen(plan_dir)
            self.assertIsNone(gen._extract_user_requirement())

    def test_no_dimensions_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            plan_dir.joinpath("interview.json").write_text(
                json.dumps({"status": "complete"}, ensure_ascii=False)
            )
            gen = _build_gen(plan_dir)
            self.assertIsNone(gen._extract_user_requirement())

    def test_dimensions_returned_as_json(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            dims = {
                "background": "calculator 缺少减法",
                "goals": "1. 新增 subtract; 2. 测试全绿",
            }
            plan_dir.joinpath("interview.json").write_text(
                json.dumps({"dimensions": dims}, ensure_ascii=False)
            )
            gen = _build_gen(plan_dir)
            result = gen._extract_user_requirement()
            self.assertIsNotNone(result)
            # Round-trip the JSON to verify structure
            parsed = json.loads(result)
            self.assertEqual(parsed["background"], "calculator 缺少减法")
            self.assertEqual(parsed["goals"], "1. 新增 subtract; 2. 测试全绿")

    def test_long_dims_capped_with_ellipsis(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            # Generate a dimensions block > 4000 chars
            big_bg = "x" * 5000
            dims = {"background": big_bg}
            plan_dir.joinpath("interview.json").write_text(
                json.dumps({"dimensions": dims}, ensure_ascii=False)
            )
            gen = _build_gen(plan_dir)
            result = gen._extract_user_requirement()
            self.assertIsNotNone(result)
            # Should be <= 4001 chars (capped + ellipsis)
            self.assertLessEqual(len(result), 4001)
            self.assertTrue(result.endswith("…"))

    def test_malformed_interview_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            plan_dir.joinpath("interview.json").write_text(
                "{not valid json"
            )
            gen = _build_gen(plan_dir)
            self.assertIsNone(gen._extract_user_requirement())


if __name__ == "__main__":
    unittest.main()