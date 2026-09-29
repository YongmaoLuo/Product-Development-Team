"""
backend.agent 模块加载测试
================================================================

2026-09-13 contract update: the ``_flatten_model_map_for_subagent``
helper and the nested tier→provider→sub_role model_map schema it
served were REMOVED (model management is delegated ENTIRELY to CC
Switch — the dispatch walk forwards the provider row's own
ANTHROPIC_* env block verbatim). The five flatten boundary tests that
used to live here were deleted with it; this file now pins only the
foundational contract: ``backend/agent.py`` imports cleanly and
exposes the public entry points the rest of the system depends on.

TDD spec:
    1. test_module_loads_without_error:
       import backend.agent as m; assert hasattr(m, 'autonomous_coding')
       验证模块语法 + 依赖
    2. test_flatten_model_map_removed:
       the legacy helper is NOT re-introduced — a regression that
       re-adds a module-level ``_flatten_model_map_for_subagent``
       re-opens the tiered model_map schema this refactor removed.
"""

import subprocess
import sys
from pathlib import Path

# Ensure backend/ is on sys.path so `import agent` works regardless of
# which test runner entry point is used (pytest from backend/, from
# project root, or via the backend subprocess).
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def test_module_loads_without_error():
    """Verify `import agent` resolves and exposes the public API.

    This is the foundational test: if the module's syntax is broken or a
    direct dependency is missing, this test catches it before any further
    schema work. The earlier task-7 implementation patched
    ``AutonomousAgent.__init__`` in its integration tests, hiding the
    real module-load failures.

    We run the import in a subprocess so the parent's ``sys.modules`` is
    not disturbed. Previous versions popped and re-imported modules in
    process, which created a second ``agent`` module object; later tests
    that held module-level references to the original ``agent`` could not
    be patched correctly (e.g. ``patch("agent.select_provider")``
    targeted the wrong module object).
    """
    backend_dir = str(_BACKEND_DIR)
    script = f"""
import sys
sys.path.insert(0, {backend_dir!r})

import agent

assert hasattr(agent, "autonomous_coding"), (
    "backend.agent must expose the autonomous_coding entry point "
    "at module level (cli.py / API endpoints depend on it)"
)
assert callable(agent.autonomous_coding), (
    "agent.autonomous_coding must be callable (it's the public API)"
)
assert not hasattr(agent, "_flatten_model_map_for_subagent"), (
    "agent must NOT expose _flatten_model_map_for_subagent — the "
    "tiered model_map schema was removed 2026-09-13 (CC Switch "
    "owns model management; see tests/unit/test_model_management_removed.py)"
)
assert hasattr(agent, "AutonomousAgent"), (
    "agent must expose AutonomousAgent class"
)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_BACKEND_DIR.parent),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"agent module failed to import in subprocess:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


def test_flatten_model_map_removed():
    """``_flatten_model_map_for_subagent`` must not be re-introduced.

    Model management is delegated ENTIRELY to CC Switch (2026-09-13):
    the dispatch walk forwards the provider row's own ANTHROPIC_* env
    block verbatim, so no backend-side tier flatten is needed. A regression
    that re-adds this helper would re-open the nested
    tier→provider→sub_role schema and silently re-enable model
    management inside the workflow.
    """
    import agent

    assert not hasattr(agent, "_flatten_model_map_for_subagent"), (
        "_flatten_model_map_for_subagent was removed 2026-09-13; "
        "do not re-introduce it — CC Switch owns model management"
    )
