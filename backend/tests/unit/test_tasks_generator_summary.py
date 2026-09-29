"""Unit tests for the tasks_generator v15 path-only contract.

M1 (commit 3fb6517) migrated query_json to interactive mode so
the LLM can use the Read tool. v15 reverts the upstream-doc
summariser to v11's path-only approach: every upstream doc is
referenced by path, and the LLM must Read it itself.

These tests pin the path-only contract:
  * No body content is inlined in any summariser
  * Every summary is path + "use Read 工具逐步阅读" hint
  * Missing upstream docs return None / "" (no fallback body)
  * The path matches the actual file the doc was loaded from
    (prd.json > prd.md > nothing)
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _build_gen(plan_dir: Path = None):
    """Build a TasksGenerator with a stub coding_tool."""
    from tasks_generator import TasksGenerator

    gen = TasksGenerator.__new__(TasksGenerator)
    gen.coding_tool = MagicMock()
    if plan_dir is not None:
        gen.plan_dir = plan_dir
        gen.prd_json = plan_dir / "prd.json"
        gen.prd_md = plan_dir / "prd.md"
    return gen


# ---------------------------------------------------------------------------
# 1. _format_doc_reference — explicit per-doc hint
# ---------------------------------------------------------------------------


class TestFormatDocReference(unittest.TestCase):
    def test_emits_path_and_read_one_dp_hint(self):
        from tasks_generator import TasksGenerator
        ref = TasksGenerator._format_doc_reference(
            "PRD 文档", Path("/tmp/prd.json")
        )
        self.assertIn("/tmp/prd.json", ref)
        # The user explicitly wants the LLM to Read one DP at a time
        # so the LLM never has to load the full doc into its context.
        self.assertIn("Read", ref)
        self.assertIn("一次", ref)


# ---------------------------------------------------------------------------
# 2. _summarise_prd / arch / test — path-only, no body content
# ---------------------------------------------------------------------------


class TestSummariseOnlyPath(unittest.TestCase):
    def test_prd_emits_only_path_no_body(self):
        # v15: even with a tiny PRD, we emit ONLY the path hint.
        # The LLM is expected to Read the file itself (M1 makes
        # this work — the LLM has tool access in interactive mode).
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            plan_dir.joinpath("prd.json").write_text(
                '{"requirement": "tiny"}'
            )
            (plan_dir / "prd.md").unlink(missing_ok=True)
            gen = _build_gen(plan_dir)
            summary = gen._summarise_prd()
            # The summary must NOT include the body content.
            self.assertNotIn("tiny", summary)
            self.assertNotIn("requirement", summary)
            # It must include the file path so the LLM knows where
            # to Read from.
            self.assertIn(plan_dir.joinpath("prd.json").as_posix(), summary)

    def test_prd_prefers_prd_json_when_both_exist(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            plan_dir.joinpath("prd.json").write_text("{}")
            plan_dir.joinpath("prd.md").write_text("# fallback content")
            gen = _build_gen(plan_dir)
            summary = gen._summarise_prd()
            # When both exist, the JSON path is the canonical source.
            self.assertIn(plan_dir.joinpath("prd.json").as_posix(), summary)
            self.assertNotIn(plan_dir.joinpath("prd.md").as_posix(), summary)

    def test_prd_falls_back_to_prd_md_when_json_missing(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            plan_dir.joinpath("prd.md").write_text("# fallback content")
            # No prd.json — the loader must use prd.md's path.
            gen = _build_gen(plan_dir)
            summary = gen._summarise_prd()
            self.assertIn(plan_dir.joinpath("prd.md").as_posix(), summary)

    def test_prd_empty_when_no_file(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            gen = _build_gen(plan_dir)
            self.assertEqual(gen._summarise_prd(), "")

    def test_arch_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            gen = _build_gen(plan_dir)
            self.assertIsNone(gen._summarise_arch_design())

    def test_arch_returns_path_only(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            plan_dir.joinpath("arch-design.md").write_text("# big body")
            gen = _build_gen(plan_dir)
            summary = gen._summarise_arch_design()
            self.assertNotIn("big body", summary)
            self.assertIn(plan_dir.joinpath("arch-design.md").as_posix(), summary)

    def test_test_design_returns_path_only(self):
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            plan_dir.joinpath("test-design.md").write_text("# big body")
            gen = _build_gen(plan_dir)
            summary = gen._summarise_test_design()
            self.assertNotIn("big body", summary)
            self.assertIn(plan_dir.joinpath("test-design.md").as_posix(), summary)


# ---------------------------------------------------------------------------
# 3. Prompt budget: never allow upstream body to inflate prompt
# ---------------------------------------------------------------------------


class TestPromptStaysSmall(unittest.TestCase):
    def test_prompt_size_independent_of_upstream_body_size(self):
        """Even a 200 KB upstream doc should produce a sub-2 KB
        path-only summary (path + Read hint). The bounded size is
        the whole point of the v15 path-only contract — the LLM
        fetches the actual content via Read tool."""
        with tempfile.TemporaryDirectory() as td:
            plan_dir = Path(td)
            plan_dir.joinpath("prd.json").write_text("p" * 200_000)
            plan_dir.joinpath("arch-design.md").write_text("a" * 200_000)
            plan_dir.joinpath("test-design.md").write_text("t" * 200_000)
            gen = _build_gen(plan_dir)
            prd = gen._summarise_prd()
            arch = gen._summarise_arch_design()
            test = gen._summarise_test_design()
            for label, s in (("prd", prd), ("arch", arch), ("test", test)):
                self.assertLess(
                    len(s), 2_000,
                    msg=f"{label} summary too large: {len(s)}",
                )


if __name__ == "__main__":
    unittest.main()