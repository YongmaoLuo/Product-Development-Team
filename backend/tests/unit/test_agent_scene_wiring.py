"""Unit tests for scene wiring on the agent execution path (subtask 4).

Contract (2026-09-13 provider-routing feature):

  * ``AutonomousAgent.plan()`` queries the LLM with ``scene="planning"``.
  * ``TaskRefiner.refine()`` queries with ``scene="refiner"`` — the
    user-pinned "post-failure replanning uses the strong tier" rule.
  * ``AutonomousAgent._audit_task_second_pass`` queries with
    ``scene="audit_second_pass"`` (replacing the old dead
    ``model_type="haiku"`` hint).
  * Task execution queries forward the task's scene: ``execution`` for
    plain tasks. (``model_type`` stays as inert metadata per the project's
    Q3=A decision.)
  * Verification repair-task generation entry
    (``repair_generator.generate_repair_tasks``) builds its default tool
    with ``scene="repair_generation"``.
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import coding_tool as coding_tool_module
from coding_tool import CodingTool


class _RecordingTool(CodingTool):
    """Minimal CodingTool subclass that records per-call scene kwarg."""

    def __init__(self):
        self.calls = []

    def query(self, prompt, system_instruction=None, retries=3,
              timeout=None, model_type=None, scene=None):
        self.calls.append({"method": "query", "scene": scene,
                           "model_type": model_type})
        return "ok"

    def query_json(self, prompt, system_instruction=None, retries=3,
                   timeout=None, model_type=None, allowed_tools=None,
                   scene=None):
        self.calls.append({"method": "query_json", "scene": scene,
                           "model_type": model_type})
        return {"tasks": []}


@pytest.mark.unit
class TestAgentPlanScene:
    def test_plan_uses_planning_scene(self):
        from agent import AutonomousAgent

        agent = AutonomousAgent.__new__(AutonomousAgent)
        agent.requirement = "demo"
        agent.coding_tool = _RecordingTool()
        agent.config = mock.MagicMock()
        agent.config.planner_system_prompt = "plan!"
        agent.config.domain_knowledge = ""
        agent.task_manager = mock.MagicMock()
        agent.logger = None

        agent.plan()

        assert agent.coding_tool.calls, "plan() must query the LLM"
        assert agent.coding_tool.calls[0]["scene"] == "planning"


@pytest.mark.unit
class TestRefinerScene:
    def test_refine_uses_refiner_scene(self):
        from refiner import TaskRefiner

        tool = _RecordingTool()
        config = mock.MagicMock()
        config.refiner_system_prompt = "refine!"

        validator = mock.MagicMock()
        report = mock.MagicMock()
        report.status = "passed"
        validator.validate.return_value = report
        validator.auto_fix.side_effect = lambda snapshot: snapshot

        refiner = TaskRefiner(tool, config, project_dir=".")

        with mock.patch("refiner.TaskOutputValidator", return_value=validator), \
             mock.patch("refiner.watchdog_signal"):
            refiner.refine(
                requirement="demo",
                tasks=[{"id": "1", "title": "t", "description": "d",
                        "test_command": "true", "status": "pending"}],
                last_coder_response="tried something",
                last_result="tests failed",
                exit_code=1,
                last_task_id="1",
            )

        assert tool.calls, "refine() must query the LLM"
        assert tool.calls[0]["scene"] == "refiner"


@pytest.mark.unit
class TestAuditSecondPassScene:
    def test_audit_second_pass_uses_audit_scene(self):
        """The old call passed model_type="haiku" (a dead hint); the new
        call must pass scene="audit_second_pass"."""
        import agent as agent_module
        import inspect

        src = inspect.getsource(agent_module.AutonomousAgent._audit_task_second_pass)
        assert 'scene="audit_second_pass"' in src, (
            "_audit_task_second_pass must query with scene='audit_second_pass'"
        )
        assert 'model_type="haiku"' not in src, (
            "the dead model_type='haiku' hint must be removed"
        )


@pytest.mark.unit
class TestRepairGenerationScene:
    def test_generate_repair_tasks_default_tool_has_scene(self):
        import inspect
        import repair_generator

        src = inspect.getsource(repair_generator.generate_repair_tasks)
        assert 'scene="repair_generation"' in src, (
            "generate_repair_tasks' default coding tool must be built "
            "with scene='repair_generation'"
        )


@pytest.mark.unit
class TestTaskExecutionScene:
    def test_task_execution_forwards_execution_scene(self):
        """The executor's per-task query passes scene='execution'.

        Static contract on agent.py source: the two coder_response query
        call sites in _execute_task_with_retry must carry
        scene="execution" (model_type stays as inert metadata).
        """
        import inspect
        import agent as agent_module

        src = inspect.getsource(agent_module)
        region = src[src.find("coder_response = self.coding_tool.query("):]
        # Both background and foreground call sites.
        count = region.count('scene="execution"')
        assert count >= 2, (
            f"expected scene='execution' on the per-task query sites, "
            f"found {count}"
        )
