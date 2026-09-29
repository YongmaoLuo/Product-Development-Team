"""Unit tests for scene annotation on server workflow phases (subtask 3).

Contract (2026-09-13 provider-routing feature):

  * ``create_coding_tool(..., scene=...)`` forwards the scene to the
    constructed ``ClaudeCodingTool`` (vendor-c/opencode branches ignore it —
    they have no provider routing).
  * Every server workflow endpoint creates its coding tool with the
    scene matching the phase it drives:

      interview            → interview
      generate/regenerate prd → prd
      review item (revise) → prd_refine / arch_refine / test_refine
      refine_prd           → prd_refine
      generate/regenerate arch → arch
      refine_arch          → arch_refine
      generate/regenerate test → test_design
      refine_test_design   → test_refine
      add_*_decision_point → prd / arch / test_design
      generate_tasks       → tasks_generation
      preflight / validate_task_workspace → tasks_generation
      verification rounds / start_verification → verification
      _run_auto_verification_loop repair-generation tool → verification

  * The legacy no-scene call ``create_coding_tool()`` still constructs
    a tool with ``scene is None`` (backward compatible for any caller
    not yet annotated).
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool as coding_tool_module
from coding_tool import ClaudeCodingTool, create_coding_tool


@pytest.mark.unit
class TestCreateCodingToolScene:
    def test_factory_forwards_scene_to_claude_tool(self):
        with mock.patch.object(
            coding_tool_module, "ClaudeCodingTool", wraps=coding_tool_module.ClaudeCodingTool
        ) as spy:
            tool = create_coding_tool(scene="prd")
            assert tool.scene == "prd"

    def test_factory_no_scene_yields_none(self):
        tool = create_coding_tool()
        assert tool.scene is None

    def test_factory_non_claude_ignores_scene(self):
        # VendorCCodingTool has no scene concept; must not crash.
        with mock.patch.object(coding_tool_module, "VendorCCodingTool") as _k:
            tool = create_coding_tool(tool_type="vendor-c", scene="prd")
            assert tool is not None


@pytest.mark.unit
class TestServerSceneAnnotation:
    """Static-contract tests: server.py source must annotate each phase's
    create_coding_tool call with the right scene.

    Rationale for source inspection instead of endpoint round-trips: the
    endpoints need plan directories / DB state to even construct; the
    routing contract we care about is "which scene literal is attached
    to which phase's tool creation", which is exactly what the source
    pins. The per-call routing behaviour itself is covered by
    test_coding_tool_scene_routing.py against the real implementation.
    """

    @staticmethod
    def _server_source() -> str:
        """``server.py`` plus every module extracted out of it.

        The workflow-phase handlers moved to ``backend/routes/phases.py`` on
        2026-09-25, and the verification loop (whose repair-generation tool
        carries ``scene="verification"``) moved to ``backend/verification_loop.py``
        in the same pass. This contract is about which ``scene=`` a phase's
        tool creation carries, which is independent of which file the handler
        lives in — so the scan follows the code.
        """
        backend = Path(coding_tool_module.__file__).parent
        parts = [(backend / "server.py").read_text(encoding="utf-8")]
        parts += [
            p.read_text(encoding="utf-8")
            for p in sorted((backend / "routes").glob("*.py"))
        ]
        loop = backend / "verification_loop.py"
        if loop.exists():
            parts.append(loop.read_text(encoding="utf-8"))
        return "\n\n".join(parts)

    def _calls_with_args(self):
        """Extract create_coding_tool(...) calls with their argument text.

        The naive ``[^)]*`` regex truncates at nested parens (e.g.
        ``str(project_dir)``), so extraction splits on the call's opening
        paren and grabs a generous fixed window instead.
        """
        import re

        src = self._server_source()
        return re.findall(r"create_coding_tool\((.{0,120})", src, re.S)

    def _assert_annotation(self, func_name: str, expected_scene: str):
        import re

        src = self._server_source()
        # Locate the function body, then the first create_coding_tool call in it.
        m = re.search(rf"^def {func_name}\(.*?(?=^def |\Z)", src, re.M | re.S)
        assert m, f"server.py function {func_name} not found"
        body = m.group(0)
        calls = re.findall(r"create_coding_tool\((.{0,120})", body, re.S)
        assert calls, f"{func_name} does not call create_coding_tool"
        assert any(
            f'scene="{expected_scene}"' in c for c in calls
        ), (
            f"{func_name} must create its coding tool with scene=\"{expected_scene}\"; "
            f"got args: {calls}"
        )

    def test_interview_uses_interview_scene(self):
        self._assert_annotation("start_interview", "interview")

    def test_continue_interview_uses_interview_scene(self):
        self._assert_annotation("continue_interview", "interview")

    def test_generate_prd_uses_prd_scene(self):
        self._assert_annotation("generate_prd", "prd")

    def test_regenerate_prd_uses_prd_scene(self):
        self._assert_annotation("regenerate_prd", "prd")

    def test_refine_prd_uses_prd_refine_scene(self):
        self._assert_annotation("refine_prd", "prd_refine")

    def test_generate_arch_uses_arch_scene(self):
        self._assert_annotation("generate_arch", "arch")

    def test_regenerate_arch_uses_arch_scene(self):
        self._assert_annotation("regenerate_arch", "arch")

    def test_refine_arch_uses_arch_refine_scene(self):
        self._assert_annotation("refine_arch", "arch_refine")

    def test_generate_test_design_uses_test_design_scene(self):
        self._assert_annotation("generate_test_design", "test_design")

    def test_refine_test_design_uses_test_refine_scene(self):
        self._assert_annotation("refine_test_design", "test_refine")

    def test_generate_tasks_uses_tasks_generation_scene(self):
        self._assert_annotation("generate_tasks", "tasks_generation")

    def test_start_verification_uses_verification_scene(self):
        self._assert_annotation("start_verification", "verification")

    def test_auto_verification_loop_uses_verification_scene(self):
        # 2026-09-18 C3: the public entry point is now a thin
        # try/finally reap wrapper; the loop body — and therefore the
        # ``create_coding_tool(..., scene="verification")`` call this
        # test pins — lives in the inner function.
        self._assert_annotation("_run_auto_verification_loop_inner", "verification")

    def test_review_revise_endpoints_annotate_refine_scenes(self):
        src = self._server_source()
        # review_item / review_arch_item / review_test_item revise branches
        for func, scene in [
            ("review_item", "prd_refine"),
            ("review_arch_item", "arch_refine"),
            ("review_test_item", "test_refine"),
        ]:
            import re
            m = re.search(rf"^def {func}\(.*?(?=^def |\Z)", src, re.M | re.S)
            assert m, f"{func} not found"
            calls = re.findall(r"create_coding_tool\((.{0,120})", m.group(0), re.S)
            if calls:  # only annotated when the endpoint creates a tool
                assert any(f'scene="{scene}"' in c for c in calls), (
                    f"{func} must annotate scene=\"{scene}\""
                )

    def test_add_decision_points_annotate_phase_scenes(self):
        src = self._server_source()
        for func, scene in [
            ("add_prd_decision_point", "prd"),
            ("add_arch_decision_point", "arch"),
            ("add_test_decision_point", "test_design"),
        ]:
            import re
            m = re.search(rf"^def {func}\(.*?(?=^def |\Z)", src, re.M | re.S)
            assert m, f"{func} not found"
            calls = re.findall(r"create_coding_tool\((.{0,120})", m.group(0), re.S)
            if calls:
                assert any(f'scene="{scene}"' in c for c in calls), (
                    f"{func} must annotate scene=\"{scene}\""
                )
