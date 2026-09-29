"""Unit tests for model-management removal + verification scene wiring
(subtask 5, Q3=A contract).

2026-09-13: The backend manages PROVIDERS only. Model management is
delegated entirely to CC Switch. Concretely:

  * ``ClaudeCodingTool._resolve_model`` (complex/medium tier model
    picking from model_map) must be GONE — the scene/provider routing
    plus the CC Switch row's own env block are the only routing levers.
  * ``agent._flatten_model_map_for_subagent`` must be GONE.
  * ``verification_dag.load_model_complexity_map`` /
    ``get_model_for_complexity`` (the never-consumed dead config) must
    be GONE.
  * The SubagentConfig written settings tmpfile must NOT carry tiered
    ``ANTHROPIC_DEFAULT_*_MODEL`` keys derived from model_map — the
    subprocess inherits model env from the CC Switch row via the
    dispatch walk instead.
  * VerificationSubAgent's scoped_tool must route by scene: the
    ``model_complexity`` hint becomes a REAL scene
    (``verification_simple`` → low tier was REMOVED per
    both simple and complex VPs use ``scene="verification"``, at least
    medium).
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.mark.unit
class TestModelManagementRemoved:
    def test_resolve_model_is_gone(self):
        from coding_tool import ClaudeCodingTool

        assert not hasattr(ClaudeCodingTool, "_resolve_model"), (
            "_resolve_model must be removed — model tiers are no longer "
            "a routing lever"
        )

    def test_flatten_model_map_is_gone(self):
        import agent

        assert not hasattr(agent, "_flatten_model_map_for_subagent"), (
            "_flatten_model_map_for_subagent must be removed"
        )

    def test_verification_dag_complexity_map_is_gone(self):
        import verification_dag

        assert not hasattr(verification_dag, "load_model_complexity_map")
        assert not hasattr(verification_dag, "get_model_for_complexity")

    def test_coding_yaml_has_no_complexity_map_section(self):
        import yaml

        cfg_path = Path(__file__).parent.parent.parent / "configs" / "coding.yaml"
        if not cfg_path.exists():
            pytest.skip("coding.yaml not present")
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        model_map = (data or {}).get("model_map") or {}
        assert "model_complexity_map" not in model_map, (
            "model_complexity_map is dead config — remove it from coding.yaml"
        )

    def test_base_yaml_has_no_model_map_section(self):
        import yaml

        cfg_path = Path(__file__).parent.parent.parent / "configs" / "_base.yaml"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert "model_map" not in (data or {}), (
            "_base.yaml model_map must be removed — CC Switch owns models"
        )


@pytest.mark.unit
class TestVerificationSceneWiring:
    def test_verification_agent_passes_verification_scene(self):
        """The VP runner maps model_complexity hints to the single
        ``verification`` scene ( no verification_simple
        tier — verification is at least medium)."""
        import inspect
        import verification_agent as va

        src = inspect.getsource(va.VerificationAgent)
        assert 'scene="verification"' in src, (
            "VerificationAgent must wire scene='verification' into the "
            "sub-agent path"
        )

    def test_verification_subagent_scoped_tool_carries_scene(self):
        import inspect
        import verification_subagent as vs

        src = inspect.getsource(vs.VerificationSubAgent)
        # The scoped tool clone must forward the sub-agent's scene into
        # ClaudeCodingTool so the VP's LLM calls route via the scene.
        assert "scene=self.scene" in src, (
            "VerificationSubAgent's scoped tool must carry "
            "scene=self.scene"
        )

    def test_summarizer_uses_summarizer_scene(self):
        import inspect
        import verification_subagent as vs

        src = inspect.getsource(vs)
        # The stuck-agent summarizer call must route via the summarizer
        # scene (low tier).
        assert 'scene="summarizer"' in src
